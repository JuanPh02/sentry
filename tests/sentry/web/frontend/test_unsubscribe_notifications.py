import abc

from sentry.incidents.models import IncidentSubscription
from sentry.models.groupsubscription import GroupSubscription
from sentry.testutils.cases import TestCase
from sentry.testutils.silo import region_silo_test
from sentry.utils.linksign import generate_signed_link


class Base:
    class UnsubscribeNotificationsBase(TestCase):
        def create_instance(self):
            raise NotImplementedError()

        @abc.abstractproperty
        def view_name(self):
            raise NotImplementedError()

        @abc.abstractmethod
        def assert_unsubscribed(self, instance, user):
            raise NotImplementedError()

        def test_renders(self):
            instance = self.create_instance()
            path = generate_signed_link(user=self.user, viewname=self.view_name, args=[instance.id])

            resp = self.client.get(path)
            assert resp.status_code == 200

        def test_process(self):
            instance = self.create_instance()
            path = generate_signed_link(user=self.user, viewname=self.view_name, args=[instance.id])

            resp = self.client.post(path, data={"op": "unsubscribe"})
            assert resp.status_code == 302
            self.assert_unsubscribed(instance, self.user)

        def test_no_access(self):
            user = self.create_user("foo@example.com")
            instance = self.create_instance()
            path = generate_signed_link(user=user, viewname=self.view_name, args=[instance.id])

            resp = self.client.get(path)
            assert resp.status_code == 404

        def test_invalid_issue(self):
            path = generate_signed_link(user=self.user, viewname=self.view_name, args=[13413434])

            resp = self.client.get(path)
            assert resp.status_code == 404


@region_silo_test(stable=True)
class UnsubscribeIssueNotificationsTest(Base.UnsubscribeNotificationsBase):
    view_name = "sentry-account-email-unsubscribe-issue"

    def create_instance(self):
        group = self.create_group()
        GroupSubscription.objects.create(
            project=self.project, group=group, user_id=self.user.id, is_active=True
        )
        return group

    def assert_unsubscribed(self, instance, user):
        assert GroupSubscription.objects.filter(
            user_id=user.id, group=instance, is_active=False
        ).exists()


@region_silo_test(stable=True)
class UnsubscribeIncidentNotificationsTest(Base.UnsubscribeNotificationsBase):
    view_name = "sentry-account-email-unsubscribe-incident"

    def create_instance(self):
        return self.create_incident()

    def assert_unsubscribed(self, instance, user):
        assert not IncidentSubscription.objects.filter(incident=instance, user_id=user.id).exists()
