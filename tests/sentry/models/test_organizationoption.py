from sentry.models.options.organization_option import OrganizationOption
from sentry.testutils.cases import TestCase
from sentry.testutils.silo import region_silo_test


@region_silo_test(stable=True)
class OrganizationOptionManagerTest(TestCase):
    def test_set_value(self):
        OrganizationOption.objects.set_value(self.organization, "foo", "bar")
        assert (
            OrganizationOption.objects.get(organization=self.organization, key="foo").value == "bar"
        )

    def test_get_value(self):
        result = OrganizationOption.objects.get_value(self.organization, "foo")
        assert result is None

        OrganizationOption.objects.create(organization=self.organization, key="foo", value="bar")
        result = OrganizationOption.objects.get_value(self.organization, "foo")
        assert result == "bar"

    def test_unset_value(self):
        OrganizationOption.objects.unset_value(self.organization, "foo")
        OrganizationOption.objects.create(organization=self.organization, key="foo", value="bar")
        OrganizationOption.objects.unset_value(self.organization, "foo")
        assert not OrganizationOption.objects.filter(
            organization=self.organization, key="foo"
        ).exists()

    def test_get_value_bulk(self):
        result = OrganizationOption.objects.get_value_bulk([self.organization], "foo")
        assert result == {self.organization: None}

        OrganizationOption.objects.create(organization=self.organization, key="foo", value="bar")
        result = OrganizationOption.objects.get_value_bulk([self.organization], "foo")
        assert result == {self.organization: "bar"}
