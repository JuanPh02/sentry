import logging
import operator
from datetime import timedelta

from django.conf import settings
from django.db import router, transaction
from django.utils import timezone
from rest_framework import serializers
from snuba_sdk import Column, Condition, Limit, Op

from sentry import features
from sentry.api.fields.actor import ActorField
from sentry.api.serializers.rest_framework.base import CamelSnakeModelSerializer
from sentry.api.serializers.rest_framework.environment import EnvironmentField
from sentry.api.serializers.rest_framework.project import ProjectField
from sentry.exceptions import InvalidSearchQuery, UnsupportedQuerySubscription
from sentry.incidents.logic import (
    CRITICAL_TRIGGER_LABEL,
    WARNING_TRIGGER_LABEL,
    ChannelLookupTimeoutError,
    check_aggregate_column_support,
    create_alert_rule,
    delete_alert_rule_trigger,
    get_column_from_aggregate,
    query_datasets_to_type,
    translate_aggregate_field,
    update_alert_rule,
)
from sentry.incidents.models import AlertRule, AlertRuleThresholdType, AlertRuleTrigger
from sentry.snuba.dataset import Dataset
from sentry.snuba.entity_subscription import (
    ENTITY_TIME_COLUMNS,
    get_entity_key_from_query_builder,
    get_entity_subscription,
)
from sentry.snuba.models import QuerySubscription, SnubaQuery, SnubaQueryEventType
from sentry.snuba.tasks import build_query_builder

from ...snuba.metrics.naming_layer.mapping import is_mri
from . import (
    CRASH_RATE_ALERTS_ALLOWED_TIME_WINDOWS,
    QUERY_TYPE_VALID_DATASETS,
    QUERY_TYPE_VALID_EVENT_TYPES,
    UNSUPPORTED_QUERIES,
)
from .alert_rule_trigger import AlertRuleTriggerSerializer

logger = logging.getLogger(__name__)


class AlertRuleSerializer(CamelSnakeModelSerializer):
    """
    Serializer for creating/updating an alert rule. Required context:
     - `organization`: The organization related to this alert rule.
     - `access`: An access object (from `request.access`)
     - `user`: The user from `request.user`
    """

    environment = EnvironmentField(required=False, allow_null=True)
    # TODO: These might be slow for many projects, since it will query for each
    # individually. If we find this to be a problem then we can look into batching.
    projects = serializers.ListField(child=ProjectField(scope="project:read"), required=False)
    excluded_projects = serializers.ListField(
        child=ProjectField(scope="project:read"), required=False
    )
    triggers = serializers.ListField(required=True)
    query_type = serializers.IntegerField(required=False)
    dataset = serializers.CharField(required=False)
    event_types = serializers.ListField(child=serializers.CharField(), required=False)
    query = serializers.CharField(required=True, allow_blank=True)
    time_window = serializers.IntegerField(
        required=True, min_value=1, max_value=int(timedelta(days=1).total_seconds() / 60)
    )
    threshold_period = serializers.IntegerField(default=1, min_value=1, max_value=20)
    comparison_delta = serializers.IntegerField(
        required=False,
        min_value=1,
        max_value=int(timedelta(days=89).total_seconds() / 60),
        allow_null=True,
    )
    aggregate = serializers.CharField(required=True, min_length=1)
    owner = ActorField(
        required=False,
        allow_null=True,
        as_actor=True,
    )  # This will be set to required=True once the frontend starts sending it.

    class Meta:
        model = AlertRule
        fields = [
            "name",
            "owner",
            "query_type",
            "dataset",
            "query",
            "time_window",
            "environment",
            "threshold_type",
            "resolve_threshold",
            "threshold_period",
            "comparison_delta",
            "aggregate",
            "projects",
            "include_all_projects",
            "excluded_projects",
            "triggers",
            "event_types",
        ]
        extra_kwargs = {
            "name": {"min_length": 1, "max_length": 64},
            "include_all_projects": {"default": False},
            "threshold_type": {"required": True},
            "resolve_threshold": {"required": False},
        }

    threshold_translators = {
        AlertRuleThresholdType.ABOVE: lambda threshold: threshold + 100,
        AlertRuleThresholdType.BELOW: lambda threshold: 100 - threshold,
    }

    def validate_owner(self, owner):
        # owner should be team:id or user:id
        if owner is None:
            return

        return owner

    def validate_query(self, query):
        query_terms = query.split()
        for query_term in query_terms:
            if query_term in UNSUPPORTED_QUERIES:
                raise serializers.ValidationError(
                    f"Unsupported Query: We do not currently support the {query_term} query"
                )
        return query

    def validate_aggregate(self, aggregate):
        try:
            allow_mri = features.has(
                "organizations:ddm-experimental",
                self.context["organization"],
                actor=self.context.get("user", None),
            )

            if not check_aggregate_column_support(
                aggregate,
                allow_mri=allow_mri,
            ):
                raise serializers.ValidationError(
                    "Invalid Metric: We do not currently support this field."
                )
        except InvalidSearchQuery as e:
            raise serializers.ValidationError(f"Invalid Metric: {e}")
        return translate_aggregate_field(aggregate)

    def validate_query_type(self, query_type):
        try:
            return SnubaQuery.Type(query_type)
        except ValueError:
            raise serializers.ValidationError(
                f"Invalid query type {query_type}, valid values are {[item.value for item in SnubaQuery.Type]}"
            )

    def validate_dataset(self, dataset):
        try:
            dataset = Dataset(dataset)
            if dataset in [Dataset.PerformanceMetrics, Dataset.Transactions]:
                return self._validate_performance_dataset(dataset)

            return dataset
        except ValueError:
            raise serializers.ValidationError(
                "Invalid dataset, valid values are %s" % [item.value for item in Dataset]
            )

    def validate_event_types(self, event_types):
        try:
            return [SnubaQueryEventType.EventType[event_type.upper()] for event_type in event_types]
        except KeyError:
            raise serializers.ValidationError(
                "Invalid event_type, valid values are %s"
                % [item.name.lower() for item in SnubaQueryEventType.EventType]
            )

    def validate_threshold_type(self, threshold_type):
        try:
            return AlertRuleThresholdType(threshold_type)
        except ValueError:
            raise serializers.ValidationError(
                "Invalid threshold type, valid values are %s"
                % [item.value for item in AlertRuleThresholdType]
            )

    def validate(self, data):
        """
        Performs validation on an alert rule's data.
        This includes ensuring there is either 1 or 2 triggers, which each have
        actions, and have proper thresholds set. The critical trigger should
        both alert and resolve 'after' the warning trigger (whether that means
        > or < the value depends on threshold type).
        """
        self._validate_query(data)
        query_type = data["query_type"]

        triggers = data.get("triggers", [])
        if not triggers:
            raise serializers.ValidationError("Must include at least one trigger")
        if len(triggers) > 2:
            raise serializers.ValidationError(
                "Must send 1 or 2 triggers - A critical trigger, and an optional warning trigger"
            )

        if query_type == SnubaQuery.Type.CRASH_RATE:
            data["event_types"] = []
        event_types = data.get("event_types")

        valid_event_types = QUERY_TYPE_VALID_EVENT_TYPES.get(query_type, set())
        if event_types and set(event_types) - valid_event_types:
            raise serializers.ValidationError(
                "Invalid event types for this dataset. Valid event types are %s"
                % sorted(et.name.lower() for et in valid_event_types)
            )

        for i, (trigger, expected_label) in enumerate(
            zip(triggers, (CRITICAL_TRIGGER_LABEL, WARNING_TRIGGER_LABEL))
        ):
            if trigger.get("label", None) != expected_label:
                raise serializers.ValidationError(
                    f'Trigger {i + 1} must be labeled "{expected_label}"'
                )
        threshold_type = data["threshold_type"]
        self._translate_thresholds(threshold_type, data.get("comparison_delta"), triggers, data)

        critical = triggers[0]

        self._validate_trigger_thresholds(threshold_type, critical, data.get("resolve_threshold"))

        if len(triggers) == 2:
            warning = triggers[1]
            self._validate_trigger_thresholds(
                threshold_type, warning, data.get("resolve_threshold")
            )
            self._validate_critical_warning_triggers(threshold_type, critical, warning)

        return data

    def _validate_query(self, data):
        dataset = data.setdefault("dataset", Dataset.Events)
        # If metric based crash rate alerts are enabled, coerce sessions over
        if dataset == Dataset.Sessions and features.has(
            "organizations:alert-crash-free-metrics",
            self.context["organization"],
            actor=self.context.get("user", None),
        ):
            dataset = data["dataset"] = Dataset.Metrics

        if features.has(
            "organizations:ddm-experimental",
            self.context["organization"],
            actor=self.context.get("user", None),
        ):
            column = get_column_from_aggregate(data["aggregate"])
            if is_mri(column) and dataset != Dataset.PerformanceMetrics:
                raise serializers.ValidationError(
                    "You can use an MRI only on alerts on performance metrics"
                )

        query_type = data.setdefault("query_type", query_datasets_to_type[dataset])

        valid_datasets = QUERY_TYPE_VALID_DATASETS[query_type]
        if dataset not in valid_datasets:
            raise serializers.ValidationError(
                "Invalid dataset for this query type. Valid datasets are %s"
                % sorted(dataset.name.lower() for dataset in valid_datasets)
            )

        if (
            not features.has(
                "organizations:mep-rollout-flag",
                self.context["organization"],
                actor=self.context.get("user", None),
            )
            and dataset == Dataset.PerformanceMetrics
            and query_type == SnubaQuery.Type.PERFORMANCE
        ):
            raise serializers.ValidationError(
                "This project does not have access to the `generic_metrics` dataset"
            )

        projects = data.get("projects")
        if not projects:
            # We just need a valid project id from the org so that we can verify
            # the query. We don't use the returned data anywhere, so it doesn't
            # matter which.
            projects = list(self.context["organization"].project_set.all()[:1])

        try:
            entity_subscription = get_entity_subscription(
                query_type,
                dataset=dataset,
                aggregate=data["aggregate"],
                time_window=int(timedelta(minutes=data["time_window"]).total_seconds()),
                extra_fields={
                    "org_id": projects[0].organization_id,
                    "event_types": data.get("event_types"),
                },
            )
        except UnsupportedQuerySubscription as e:
            raise serializers.ValidationError(f"{e}")

        self._validate_snql_query(data, entity_subscription, projects)

    def _validate_snql_query(self, data, entity_subscription, projects):
        end = timezone.now()
        start = end - timedelta(minutes=10)
        try:
            query_builder = build_query_builder(
                entity_subscription,
                data["query"],
                [p.id for p in projects],
                data.get("environment"),
                params={
                    "organization_id": projects[0].organization_id,
                    "project_id": [p.id for p in projects],
                    "start": start,
                    "end": end,
                },
            )
        except (InvalidSearchQuery, ValueError) as e:
            raise serializers.ValidationError(f"Invalid Query or Metric: {e}")

        if not query_builder.are_columns_resolved():
            raise serializers.ValidationError(
                "Invalid Metric: Please pass a valid function for aggregation"
            )

        dataset = Dataset(data["dataset"].value)
        self._validate_time_window(dataset, data.get("time_window"))

        time_col = ENTITY_TIME_COLUMNS[get_entity_key_from_query_builder(query_builder)]
        query_builder.add_conditions(
            [
                Condition(Column(time_col), Op.GTE, start),
                Condition(Column(time_col), Op.LT, end),
            ]
        )
        query_builder.limit = Limit(1)

        try:
            query_builder.run_query(referrer="alertruleserializer.test_query")
        except Exception:
            logger.exception("Error while validating snuba alert rule query")
            raise serializers.ValidationError(
                "Invalid Query or Metric: An error occurred while attempting " "to run the query"
            )

    def _translate_thresholds(self, threshold_type, comparison_delta, triggers, data):
        """
        Performs transformations on the thresholds used in the alert. Currently this is used to
        translate thresholds for comparison alerts. The frontend will pass in the delta percent
        change. So a 30% increase, 40% decrease, etc. We want to change this to the total percentage
        we expect when comparing values. So 130% for a 30% increase, 60% for a 40% decrease, etc.
        This makes these threshold operate in the same way as our other thresholds.
        """
        if comparison_delta is None:
            return

        translator = self.threshold_translators[threshold_type]
        resolve_threshold = data.get("resolve_threshold")
        if resolve_threshold:
            data["resolve_threshold"] = translator(resolve_threshold)
        for trigger in triggers:
            trigger["alert_threshold"] = translator(trigger["alert_threshold"])

    @staticmethod
    def _validate_time_window(dataset, time_window):
        if dataset in [Dataset.Sessions, Dataset.Metrics]:
            # Validate time window
            if time_window not in CRASH_RATE_ALERTS_ALLOWED_TIME_WINDOWS:
                raise serializers.ValidationError(
                    "Invalid Time Window: Allowed time windows for crash rate alerts are: "
                    "30min, 1h, 2h, 4h, 12h and 24h"
                )

    def _validate_trigger_thresholds(self, threshold_type, trigger, resolve_threshold):
        if resolve_threshold is None:
            return
        is_integer = (
            type(trigger["alert_threshold"]) is int or trigger["alert_threshold"].is_integer()
        ) and (type(resolve_threshold) is int or resolve_threshold.is_integer())
        # Since we're comparing non-inclusive thresholds here (>, <), we need
        # to modify the values when we compare. An example of why:
        # Alert > 0, resolve < 1. This means that we want to alert on values
        # of 1 or more, and resolve on values of 0 or less. This is valid, but
        # without modifying the values, this boundary case will fail.
        if threshold_type == AlertRuleThresholdType.ABOVE:
            alert_op = operator.le
            alert_add, resolve_add = (1, -1) if is_integer else (0, 0)
        else:
            alert_op = operator.ge
            alert_add, resolve_add = (-1, 1) if is_integer else (0, 0)

        if alert_op(trigger["alert_threshold"] + alert_add, resolve_threshold + resolve_add):
            raise serializers.ValidationError(
                f"{trigger['label']} alert threshold must be {threshold_type.name.lower()} resolution threshold"
            )

    def _validate_critical_warning_triggers(self, threshold_type, critical, warning):
        if threshold_type == AlertRuleThresholdType.ABOVE:
            alert_op = operator.lt
            threshold_type = "above"
        else:
            alert_op = operator.gt
            threshold_type = "below"

        if alert_op(critical["alert_threshold"], warning["alert_threshold"]):
            raise serializers.ValidationError(
                f"Critical trigger must have an alert threshold {threshold_type} warning trigger"
            )

    def _validate_performance_dataset(self, dataset):
        if dataset != Dataset.Transactions:
            return dataset

        has_dynamic_sampling = features.has(
            "organizations:dynamic-sampling", self.context["organization"]
        )
        has_performance_metrics_flag = features.has(
            "organizations:mep-rollout-flag", self.context["organization"]
        )
        has_performance_metrics = has_dynamic_sampling and has_performance_metrics_flag

        has_on_demand_metrics = features.has(
            "organizations:on-demand-metrics-extraction",
            self.context["organization"],
        )

        if has_performance_metrics or has_on_demand_metrics:
            raise serializers.ValidationError(
                "Performance alerts must use the `generic_metrics` dataset"
            )

        return dataset

    def create(self, validated_data):
        org_subscription_count = QuerySubscription.objects.filter(
            project__organization_id=self.context["organization"].id,
            status__in=(
                QuerySubscription.Status.ACTIVE.value,
                QuerySubscription.Status.CREATING.value,
                QuerySubscription.Status.UPDATING.value,
            ),
        ).count()

        if org_subscription_count >= settings.MAX_QUERY_SUBSCRIPTIONS_PER_ORG:
            raise serializers.ValidationError(
                f"You may not exceed {settings.MAX_QUERY_SUBSCRIPTIONS_PER_ORG} metric alerts per organization"
            )
        with transaction.atomic(router.db_for_write(AlertRule)):
            triggers = validated_data.pop("triggers")
            alert_rule = create_alert_rule(
                user=self.context.get("user", None),
                organization=self.context["organization"],
                ip_address=self.context.get("ip_address"),
                **validated_data,
            )
            self._handle_triggers(alert_rule, triggers)
            return alert_rule

    def update(self, instance, validated_data):
        triggers = validated_data.pop("triggers")
        if "id" in validated_data:
            validated_data.pop("id")
        with transaction.atomic(router.db_for_write(AlertRule)):
            alert_rule = update_alert_rule(
                instance,
                user=self.context.get("user", None),
                ip_address=self.context.get("ip_address"),
                **validated_data,
            )
            self._handle_triggers(alert_rule, triggers)
            return alert_rule

    def _handle_triggers(self, alert_rule, triggers):
        channel_lookup_timeout_error = None
        if triggers is not None:
            # Delete triggers we don't have present in the incoming data
            trigger_ids = [x["id"] for x in triggers if "id" in x]
            triggers_to_delete = AlertRuleTrigger.objects.filter(alert_rule=alert_rule).exclude(
                id__in=trigger_ids
            )
            for trigger in triggers_to_delete:
                delete_alert_rule_trigger(trigger)

            for trigger_data in triggers:
                if "id" in trigger_data:
                    trigger_instance = AlertRuleTrigger.objects.get(
                        alert_rule=alert_rule, id=trigger_data["id"]
                    )
                else:
                    trigger_instance = None

                trigger_serializer = AlertRuleTriggerSerializer(
                    context={
                        "alert_rule": alert_rule,
                        "organization": self.context["organization"],
                        "access": self.context["access"],
                        "user": self.context["user"],
                        "use_async_lookup": self.context.get("use_async_lookup"),
                        "input_channel_id": self.context.get("input_channel_id"),
                        "validate_channel_id": self.context.get("validate_channel_id", True),
                        "installations": self.context.get("installations"),
                        "integrations": self.context.get("integrations"),
                    },
                    instance=trigger_instance,
                    data=trigger_data,
                )

                if trigger_serializer.is_valid():
                    try:
                        trigger_serializer.save()
                    except ChannelLookupTimeoutError as e:
                        # raise the lookup error after the rest of the validation is complete
                        channel_lookup_timeout_error = e
                else:
                    raise serializers.ValidationError(trigger_serializer.errors)
        if channel_lookup_timeout_error:
            raise channel_lookup_timeout_error
