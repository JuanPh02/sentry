from functools import cached_property
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch
from uuid import uuid4

import pytest
import yaml
from django.core.files.storage import Storage
from google.cloud.devtools.cloudbuild_v1 import Build
from google_crc32c import value as crc32c

from sentry.backup.dependencies import NormalizedModelName, get_model_name
from sentry.backup.helpers import (
    ImportFlags,
    LocalFileDecryptor,
    LocalFileEncryptor,
    create_encrypted_export_tarball,
    decrypt_encrypted_tarball,
    unwrap_encrypted_export_tarball,
)
from sentry.backup.imports import import_in_organization_scope
from sentry.models.files.file import File
from sentry.models.files.utils import get_storage
from sentry.models.importchunk import (
    ControlImportChunk,
    ControlImportChunkReplica,
    RegionImportChunk,
)
from sentry.models.organization import Organization
from sentry.models.organizationmember import OrganizationMember
from sentry.models.relocation import (
    Relocation,
    RelocationFile,
    RelocationValidation,
    RelocationValidationAttempt,
    ValidationStatus,
)
from sentry.models.user import User
from sentry.silo.base import SiloMode
from sentry.tasks.relocation import (
    ERR_NOTIFYING_INTERNAL,
    ERR_POSTPROCESSING_INTERNAL,
    ERR_PREPROCESSING_DECRYPTION,
    ERR_PREPROCESSING_INTERNAL,
    ERR_PREPROCESSING_INVALID_JSON,
    ERR_PREPROCESSING_INVALID_TARBALL,
    ERR_PREPROCESSING_MISSING_ORGS,
    ERR_PREPROCESSING_NO_ORGS,
    ERR_PREPROCESSING_NO_USERS,
    ERR_PREPROCESSING_TOO_MANY_ORGS,
    ERR_PREPROCESSING_TOO_MANY_USERS,
    ERR_UPLOADING_FAILED,
    ERR_VALIDATING_INTERNAL,
    ERR_VALIDATING_MAX_RUNS,
    MAX_FAST_TASK_ATTEMPTS,
    MAX_FAST_TASK_RETRIES,
    MAX_VALIDATION_POLL_ATTEMPTS,
    MAX_VALIDATION_POLLS,
    LostPasswordHash,
    completed,
    importing,
    notifying_owner,
    notifying_users,
    postprocessing,
    preprocessing_baseline_config,
    preprocessing_colliding_users,
    preprocessing_complete,
    preprocessing_scan,
    uploading_complete,
    validating_complete,
    validating_poll,
    validating_start,
)
from sentry.testutils.cases import TestCase, TransactionTestCase
from sentry.testutils.factories import get_fixture_path
from sentry.testutils.helpers.backups import FakeKeyManagementServiceClient, generate_rsa_key_pair
from sentry.testutils.helpers.task_runner import BurstTaskRunner, BustTaskRunnerRetryError
from sentry.testutils.silo import assume_test_silo_mode, region_silo_test
from sentry.utils import json
from sentry.utils.relocation import RELOCATION_BLOB_SIZE, RELOCATION_FILE_TYPE

IMPORT_JSON_FILE_PATH = get_fixture_path("backup", "fresh-install.json")


class FakeCloudBuildClient:
    """
    Fake version of `CloudBuildClient` that removes the two network calls we rely on.
    """

    create_build = MagicMock()
    get_build = MagicMock()


class RelocationTaskTestCase(TestCase):
    def setUp(self):
        super().setUp()
        self.owner = self.create_user(
            email="owner@example.com", is_superuser=False, is_staff=False, is_active=True
        )
        self.superuser = self.create_user(
            email="superuser@example.com", is_superuser=True, is_staff=True, is_active=True
        )
        self.login_as(user=self.superuser, superuser=True)
        self.relocation: Relocation = Relocation.objects.create(
            creator_id=self.superuser.id,
            owner_id=self.owner.id,
            want_org_slugs=["testing"],
            step=Relocation.Step.UPLOADING.value,
        )
        self.relocation_file = RelocationFile.objects.create(
            relocation=self.relocation,
            file=self.file,
            kind=RelocationFile.Kind.RAW_USER_DATA.value,
        )
        self.uuid = str(self.relocation.uuid)

    @cached_property
    def file(self):
        with TemporaryDirectory() as tmp_dir:
            (priv_key_pem, pub_key_pem) = generate_rsa_key_pair()
            tmp_priv_key_path = Path(tmp_dir).joinpath("key")
            self.priv_key_pem = priv_key_pem
            with open(tmp_priv_key_path, "wb") as f:
                f.write(priv_key_pem)

            tmp_pub_key_path = Path(tmp_dir).joinpath("key.pub")
            self.pub_key_pem = pub_key_pem
            with open(tmp_pub_key_path, "wb") as f:
                f.write(pub_key_pem)

            with open(IMPORT_JSON_FILE_PATH, "rb") as f:
                data = json.load(f)
                with open(tmp_pub_key_path, "rb") as p:
                    file = File.objects.create(name="export.tar", type=RELOCATION_FILE_TYPE)
                    self.tarball = create_encrypted_export_tarball(
                        data, LocalFileEncryptor(p)
                    ).getvalue()
                    file.putfile(BytesIO(self.tarball))

            return file

    def swap_file(
        self, file: File, fixture_name: str, blob_size: int = RELOCATION_BLOB_SIZE
    ) -> None:
        with TemporaryDirectory() as tmp_dir:
            tmp_priv_key_path = Path(tmp_dir).joinpath("key")
            tmp_pub_key_path = Path(tmp_dir).joinpath("key.pub")
            with open(tmp_priv_key_path, "wb") as f:
                f.write(self.priv_key_pem)
            with open(tmp_pub_key_path, "wb") as f:
                f.write(self.pub_key_pem)
            with open(get_fixture_path("backup", fixture_name)) as f:
                data = json.load(f)
                with open(tmp_pub_key_path, "rb") as p:
                    self.tarball = create_encrypted_export_tarball(
                        data, LocalFileEncryptor(p)
                    ).getvalue()
                    file.putfile(BytesIO(self.tarball), blob_size=blob_size)

    def mock_kms_client(self, fake_kms_client: FakeKeyManagementServiceClient):
        fake_kms_client.asymmetric_decrypt.call_count = 0
        fake_kms_client.get_public_key.call_count = 0

        unwrapped = unwrap_encrypted_export_tarball(BytesIO(self.tarball))
        plaintext_dek = LocalFileDecryptor.from_bytes(
            self.priv_key_pem
        ).decrypt_data_encryption_key(unwrapped)

        fake_kms_client.asymmetric_decrypt.return_value = SimpleNamespace(
            plaintext=plaintext_dek,
            plaintext_crc32c=crc32c(plaintext_dek),
        )
        fake_kms_client.asymmetric_decrypt.side_effect = None

        fake_kms_client.get_public_key.return_value = SimpleNamespace(
            pem=self.pub_key_pem.decode("utf-8")
        )
        fake_kms_client.get_public_key.side_effect = None

    def mock_cloudbuild_client(
        self, fake_cloudbuild_client: FakeCloudBuildClient, status: Build.Status
    ):
        fake_cloudbuild_client.create_build.call_count = 0
        fake_cloudbuild_client.get_build.call_count = 0

        fake_cloudbuild_client.create_build.return_value = SimpleNamespace(
            metadata=SimpleNamespace(build=SimpleNamespace(id=uuid4().hex))
        )
        fake_cloudbuild_client.create_build.side_effect = None

        fake_cloudbuild_client.get_build.return_value = SimpleNamespace(status=status)
        fake_cloudbuild_client.get_build.side_effect = None

    def mock_message_builder(self, fake_message_builder: Mock):
        fake_message_builder.return_value.send_async.return_value = Mock()


@region_silo_test(stable=True)
@patch("sentry.utils.relocation.MessageBuilder")
@patch("sentry.tasks.relocation.preprocessing_scan.delay")
class UploadingCompleteTest(RelocationTaskTestCase):
    def test_success(
        self,
        preprocessing_scan_mock: Mock,
        fake_message_builder: Mock,
    ):
        self.mock_message_builder(fake_message_builder)

        uploading_complete(self.uuid)

        assert fake_message_builder.call_count == 0
        assert preprocessing_scan_mock.call_count == 1

    def test_retry_if_attempts_left(
        self,
        preprocessing_scan_mock: Mock,
        fake_message_builder: Mock,
    ):
        RelocationFile.objects.filter(relocation=self.relocation).delete()
        self.mock_message_builder(fake_message_builder)

        # An exception being raised will trigger a retry in celery.
        with pytest.raises(Exception):
            uploading_complete(self.uuid)

        assert fake_message_builder.call_count == 0
        assert preprocessing_scan_mock.call_count == 0

        relocation = Relocation.objects.get(uuid=self.uuid)
        assert relocation.status == Relocation.Status.IN_PROGRESS.value
        assert not relocation.failure_reason

    def test_fail_if_no_attempts_left(
        self,
        preprocessing_scan_mock: Mock,
        fake_message_builder: Mock,
    ):
        self.relocation.latest_task = "UPLOADING_COMPLETE"
        self.relocation.latest_task_attempts = MAX_FAST_TASK_RETRIES
        self.relocation.save()
        RelocationFile.objects.filter(relocation=self.relocation).delete()
        self.mock_message_builder(fake_message_builder)

        with pytest.raises(Exception):
            uploading_complete(self.uuid)

        assert fake_message_builder.call_count == 1
        assert fake_message_builder.call_args.kwargs["type"] == "relocation.failed"
        fake_message_builder.return_value.send_async.assert_called_once_with(
            to=[self.owner.email, self.superuser.email]
        )

        assert preprocessing_scan_mock.call_count == 0

        relocation = Relocation.objects.get(uuid=self.uuid)
        assert relocation.status == Relocation.Status.FAILURE.value
        assert relocation.failure_reason == ERR_UPLOADING_FAILED


@region_silo_test(stable=True)
@patch(
    "sentry.backup.helpers.KeyManagementServiceClient",
    new_callable=lambda: FakeKeyManagementServiceClient,
)
@patch("sentry.utils.relocation.MessageBuilder")
@patch("sentry.tasks.relocation.preprocessing_baseline_config.delay")
class PreprocessingScanTest(RelocationTaskTestCase):
    def setUp(self):
        super().setUp()
        self.relocation.step = Relocation.Step.UPLOADING.value
        self.relocation.latest_task = "UPLOADING_COMPLETE"
        self.relocation.save()

    def test_success_admin_assisted_relocation(
        self,
        preprocessing_baseline_config_mock: Mock,
        fake_message_builder: Mock,
        fake_kms_client: FakeKeyManagementServiceClient,
    ):
        self.mock_message_builder(fake_message_builder)
        self.mock_kms_client(fake_kms_client)

        preprocessing_scan(self.uuid)

        assert fake_kms_client.asymmetric_decrypt.call_count == 1
        assert fake_kms_client.get_public_key.call_count == 0

        assert fake_message_builder.call_count == 1
        assert fake_message_builder.call_args.kwargs["type"] == "relocation.started"
        fake_message_builder.return_value.send_async.assert_called_once_with(
            to=[self.owner.email, self.superuser.email]
        )

        assert preprocessing_baseline_config_mock.call_count == 1
        assert Relocation.objects.get(uuid=self.uuid).want_usernames == [
            "admin@example.com",
            "member@example.com",
        ]

    def test_success_self_service_relocation(
        self,
        preprocessing_baseline_config_mock: Mock,
        fake_message_builder: Mock,
        fake_kms_client: FakeKeyManagementServiceClient,
    ):
        self.mock_kms_client(fake_kms_client)
        self.relocation.creator_id = self.relocation.owner_id
        self.relocation.save()

        preprocessing_scan(self.uuid)

        assert fake_kms_client.asymmetric_decrypt.call_count == 1
        assert fake_kms_client.get_public_key.call_count == 0

        assert fake_message_builder.call_count == 1
        assert fake_message_builder.call_args.kwargs["type"] == "relocation.started"
        fake_message_builder.return_value.send_async.assert_called_once_with(to=[self.owner.email])

        assert preprocessing_baseline_config_mock.call_count == 1

        assert Relocation.objects.get(uuid=self.uuid).want_usernames == [
            "admin@example.com",
            "member@example.com",
        ]

    def test_retry_if_attempts_left(
        self,
        preprocessing_baseline_config_mock: Mock,
        fake_message_builder: Mock,
        fake_kms_client: FakeKeyManagementServiceClient,
    ):
        RelocationFile.objects.filter(relocation=self.relocation).delete()
        self.mock_message_builder(fake_message_builder)
        self.mock_kms_client(fake_kms_client)

        # An exception being raised will trigger a retry in celery.
        with pytest.raises(Exception):
            preprocessing_scan(self.uuid)

        assert fake_kms_client.asymmetric_decrypt.call_count == 0
        assert fake_kms_client.get_public_key.call_count == 0
        assert fake_message_builder.call_count == 0
        assert preprocessing_baseline_config_mock.call_count == 0

        relocation = Relocation.objects.get(uuid=self.uuid)
        assert relocation.status == Relocation.Status.IN_PROGRESS.value
        assert not relocation.failure_reason

    def test_fail_if_no_attempts_left(
        self,
        preprocessing_baseline_config_mock: Mock,
        fake_message_builder: Mock,
        fake_kms_client: FakeKeyManagementServiceClient,
    ):
        self.relocation.latest_task = "PREPROCESSING_SCAN"
        self.relocation.latest_task_attempts = MAX_FAST_TASK_RETRIES
        self.relocation.save()
        RelocationFile.objects.filter(relocation=self.relocation).delete()
        self.mock_message_builder(fake_message_builder)
        self.mock_kms_client(fake_kms_client)

        with pytest.raises(Exception):
            preprocessing_scan(self.uuid)

        assert fake_kms_client.asymmetric_decrypt.call_count == 0
        assert fake_kms_client.get_public_key.call_count == 0

        assert fake_message_builder.call_count == 1
        assert fake_message_builder.call_args.kwargs["type"] == "relocation.failed"
        fake_message_builder.return_value.send_async.assert_called_once_with(
            to=[self.owner.email, self.superuser.email]
        )

        assert preprocessing_baseline_config_mock.call_count == 0

        relocation = Relocation.objects.get(uuid=self.uuid)
        assert relocation.status == Relocation.Status.FAILURE.value
        assert relocation.failure_reason == ERR_PREPROCESSING_INTERNAL

    def test_fail_invalid_tarball(
        self,
        preprocessing_baseline_config_mock: Mock,
        fake_message_builder: Mock,
        fake_kms_client: FakeKeyManagementServiceClient,
    ):
        file = RelocationFile.objects.get(relocation=self.relocation).file
        corrupted_tarball_bytes = bytearray(file.getfile().read())[9:]
        file.putfile(BytesIO(bytes(corrupted_tarball_bytes)))
        self.mock_message_builder(fake_message_builder)
        self.mock_kms_client(fake_kms_client)

        preprocessing_scan(self.uuid)

        assert fake_message_builder.call_count == 1
        assert fake_message_builder.call_args.kwargs["type"] == "relocation.failed"
        fake_message_builder.return_value.send_async.assert_called_once_with(
            to=[self.owner.email, self.superuser.email]
        )

        assert preprocessing_baseline_config_mock.call_count == 0

        relocation = Relocation.objects.get(uuid=self.uuid)
        assert relocation.status == Relocation.Status.FAILURE.value
        assert relocation.failure_reason == ERR_PREPROCESSING_INVALID_TARBALL

    def test_fail_decryption_failure(
        self,
        preprocessing_baseline_config_mock: Mock,
        fake_message_builder: Mock,
        fake_kms_client: FakeKeyManagementServiceClient,
    ):
        # Add invalid 2-octet UTF-8 sequence to the returned plaintext.
        self.mock_message_builder(fake_message_builder)
        self.mock_kms_client(fake_kms_client)
        fake_kms_client.asymmetric_decrypt.return_value.plaintext += b"\xc3\x28"

        # We retry on decryption failures, just to account for flakiness on the KMS server's side.
        # Try this as the last attempt to see the actual error.
        self.relocation.latest_task = "PREPROCESSING_SCAN"
        self.relocation.latest_task_attempts = MAX_FAST_TASK_RETRIES
        self.relocation.save()

        with pytest.raises(Exception):
            preprocessing_scan(self.uuid)

        assert fake_message_builder.call_count == 1
        assert fake_message_builder.call_args.kwargs["type"] == "relocation.failed"
        fake_message_builder.return_value.send_async.assert_called_once_with(
            to=[self.owner.email, self.superuser.email]
        )

        assert fake_kms_client.asymmetric_decrypt.call_count == 1
        assert preprocessing_baseline_config_mock.call_count == 0

        relocation = Relocation.objects.get(uuid=self.uuid)
        assert relocation.status == Relocation.Status.FAILURE.value
        assert relocation.failure_reason == ERR_PREPROCESSING_DECRYPTION

    def test_fail_invalid_json(
        self,
        preprocessing_baseline_config_mock: Mock,
        fake_message_builder: Mock,
        fake_kms_client: FakeKeyManagementServiceClient,
    ):
        file = RelocationFile.objects.get(relocation=self.relocation).file
        self.swap_file(file, "invalid-user.json")
        self.mock_message_builder(fake_message_builder)
        self.mock_kms_client(fake_kms_client)

        preprocessing_scan(self.uuid)

        assert fake_message_builder.call_count == 1
        assert fake_message_builder.call_args.kwargs["type"] == "relocation.failed"
        fake_message_builder.return_value.send_async.assert_called_once_with(
            to=[self.owner.email, self.superuser.email]
        )

        assert preprocessing_baseline_config_mock.call_count == 0

        relocation = Relocation.objects.get(uuid=self.uuid)
        assert relocation.status == Relocation.Status.FAILURE.value
        assert relocation.failure_reason == ERR_PREPROCESSING_INVALID_JSON

    def test_fail_no_users(
        self,
        preprocessing_baseline_config_mock: Mock,
        fake_message_builder: Mock,
        fake_kms_client: FakeKeyManagementServiceClient,
    ):
        file = RelocationFile.objects.get(relocation=self.relocation).file
        self.swap_file(file, "single-option.json")
        self.mock_message_builder(fake_message_builder)
        self.mock_kms_client(fake_kms_client)

        preprocessing_scan(self.uuid)

        assert fake_message_builder.call_count == 1
        assert fake_message_builder.call_args.kwargs["type"] == "relocation.failed"
        fake_message_builder.return_value.send_async.assert_called_once_with(
            to=[self.owner.email, self.superuser.email]
        )

        assert preprocessing_baseline_config_mock.call_count == 0

        relocation = Relocation.objects.get(uuid=self.uuid)
        assert relocation.status == Relocation.Status.FAILURE.value
        assert relocation.failure_reason == ERR_PREPROCESSING_NO_USERS

    @patch("sentry.tasks.relocation.MAX_USERS_PER_RELOCATION", 0)
    def test_fail_too_many_users(
        self,
        preprocessing_baseline_config_mock: Mock,
        fake_message_builder: Mock,
        fake_kms_client: FakeKeyManagementServiceClient,
    ):
        self.mock_message_builder(fake_message_builder)
        self.mock_kms_client(fake_kms_client)

        preprocessing_scan(self.uuid)

        assert fake_message_builder.call_count == 1
        assert fake_message_builder.call_args.kwargs["type"] == "relocation.failed"
        fake_message_builder.return_value.send_async.assert_called_once_with(
            to=[self.owner.email, self.superuser.email]
        )

        assert preprocessing_baseline_config_mock.call_count == 0

        relocation = Relocation.objects.get(uuid=self.uuid)
        assert relocation.status == Relocation.Status.FAILURE.value
        assert relocation.failure_reason == ERR_PREPROCESSING_TOO_MANY_USERS.substitute(count=2)

    def test_fail_no_orgs(
        self,
        preprocessing_baseline_config_mock: Mock,
        fake_message_builder: Mock,
        fake_kms_client: FakeKeyManagementServiceClient,
    ):
        file = RelocationFile.objects.get(relocation=self.relocation).file
        self.swap_file(file, "user-with-minimum-privileges.json")
        self.mock_message_builder(fake_message_builder)
        self.mock_kms_client(fake_kms_client)

        preprocessing_scan(self.uuid)

        assert fake_message_builder.call_count == 1
        assert fake_message_builder.call_args.kwargs["type"] == "relocation.failed"
        fake_message_builder.return_value.send_async.assert_called_once_with(
            to=[self.owner.email, self.superuser.email]
        )

        assert preprocessing_baseline_config_mock.call_count == 0

        relocation = Relocation.objects.get(uuid=self.uuid)
        assert relocation.status == Relocation.Status.FAILURE.value
        assert relocation.failure_reason == ERR_PREPROCESSING_NO_ORGS

    @patch("sentry.tasks.relocation.MAX_ORGS_PER_RELOCATION", 0)
    def test_fail_too_many_orgs(
        self,
        preprocessing_baseline_config_mock: Mock,
        fake_message_builder: Mock,
        fake_kms_client: FakeKeyManagementServiceClient,
    ):
        self.mock_message_builder(fake_message_builder)
        self.mock_kms_client(fake_kms_client)

        preprocessing_scan(self.uuid)

        assert fake_message_builder.call_count == 1
        assert fake_message_builder.call_args.kwargs["type"] == "relocation.failed"
        fake_message_builder.return_value.send_async.assert_called_once_with(
            to=[self.owner.email, self.superuser.email]
        )

        assert preprocessing_baseline_config_mock.call_count == 0

        relocation = Relocation.objects.get(uuid=self.uuid)
        assert relocation.status == Relocation.Status.FAILURE.value
        assert relocation.failure_reason == ERR_PREPROCESSING_TOO_MANY_ORGS.substitute(count=1)

    def test_fail_missing_orgs(
        self,
        preprocessing_baseline_config_mock: Mock,
        fake_message_builder: Mock,
        fake_kms_client: FakeKeyManagementServiceClient,
    ):
        orgs = ["does-not-exist"]
        relocation = Relocation.objects.get(uuid=self.uuid)
        relocation.want_org_slugs = orgs
        relocation.save()
        self.mock_message_builder(fake_message_builder)
        self.mock_kms_client(fake_kms_client)

        preprocessing_scan(self.uuid)

        assert fake_message_builder.call_count == 1
        assert fake_message_builder.call_args.kwargs["type"] == "relocation.failed"
        fake_message_builder.return_value.send_async.assert_called_once_with(
            to=[self.owner.email, self.superuser.email]
        )

        assert preprocessing_baseline_config_mock.call_count == 0

        relocation = Relocation.objects.get(uuid=self.uuid)
        assert relocation.status == Relocation.Status.FAILURE.value
        assert relocation.failure_reason == ERR_PREPROCESSING_MISSING_ORGS.substitute(
            orgs=",".join(orgs)
        )


@region_silo_test(stable=True)
@patch(
    "sentry.backup.helpers.KeyManagementServiceClient",
    new_callable=lambda: FakeKeyManagementServiceClient,
)
@patch("sentry.utils.relocation.MessageBuilder")
@patch("sentry.tasks.relocation.preprocessing_colliding_users.delay")
class PreprocessingBaselineConfigTest(RelocationTaskTestCase):
    def setUp(self):
        super().setUp()
        self.relocation.step = Relocation.Step.PREPROCESSING.value
        self.relocation.latest_task = "PREPROCESSING_SCAN"
        self.relocation.save()

    def test_success(
        self,
        preprocessing_colliding_users_mock: Mock,
        fake_message_builder: Mock,
        fake_kms_client: FakeKeyManagementServiceClient,
    ):
        self.mock_message_builder(fake_message_builder)
        self.mock_kms_client(fake_kms_client)

        preprocessing_baseline_config(self.uuid)

        assert fake_kms_client.asymmetric_decrypt.call_count == 0
        assert fake_kms_client.get_public_key.call_count == 1
        assert fake_message_builder.call_count == 0
        assert preprocessing_colliding_users_mock.call_count == 1

        relocation_file = (
            RelocationFile.objects.filter(
                relocation=self.relocation,
                kind=RelocationFile.Kind.BASELINE_CONFIG_VALIDATION_DATA.value,
            )
            .select_related("file")
            .first()
        )
        assert relocation_file.file.name == "baseline-config.tar"

        with relocation_file.file.getfile() as fp:
            json_models = json.loads(
                decrypt_encrypted_tarball(fp, LocalFileDecryptor.from_bytes(self.priv_key_pem))
            )
        assert len(json_models) > 0

        # Only user `superuser` is an admin, so only they should be exported.
        for json_model in json_models:
            if NormalizedModelName(json_model["model"]) == get_model_name(User):
                assert json_model["fields"]["username"] in "superuser@example.com"

    def test_retry_if_attempts_left(
        self,
        preprocessing_colliding_users_mock: Mock,
        fake_message_builder: Mock,
        fake_kms_client: FakeKeyManagementServiceClient,
    ):
        self.mock_message_builder(fake_message_builder)
        self.mock_kms_client(fake_kms_client)
        RelocationFile.objects.filter(relocation=self.relocation).delete()

        # An exception being raised will trigger a retry in celery.
        with pytest.raises(Exception):
            fake_kms_client.get_public_key.side_effect = Exception("Test")

            preprocessing_baseline_config(self.uuid)

        assert fake_kms_client.asymmetric_decrypt.call_count == 0
        assert fake_kms_client.get_public_key.call_count == 1
        assert fake_message_builder.call_count == 0
        assert preprocessing_colliding_users_mock.call_count == 0

        relocation = Relocation.objects.get(uuid=self.uuid)
        assert relocation.status == Relocation.Status.IN_PROGRESS.value
        assert not relocation.failure_reason

    def test_fail_if_no_attempts_left(
        self,
        preprocessing_colliding_users_mock: Mock,
        fake_message_builder: Mock,
        fake_kms_client: FakeKeyManagementServiceClient,
    ):
        self.relocation.latest_task = "PREPROCESSING_BASELINE_CONFIG"
        self.relocation.latest_task_attempts = MAX_FAST_TASK_RETRIES
        self.relocation.save()
        RelocationFile.objects.filter(relocation=self.relocation).delete()

        self.mock_message_builder(fake_message_builder)
        self.mock_kms_client(fake_kms_client)
        fake_kms_client.get_public_key.side_effect = Exception("Test")

        with pytest.raises(Exception):
            preprocessing_baseline_config(self.uuid)

        assert fake_kms_client.asymmetric_decrypt.call_count == 0
        assert fake_kms_client.get_public_key.call_count == 1

        assert fake_message_builder.call_count == 1
        assert fake_message_builder.call_args.kwargs["type"] == "relocation.failed"
        fake_message_builder.return_value.send_async.assert_called_once_with(
            to=[self.owner.email, self.superuser.email]
        )

        assert preprocessing_colliding_users_mock.call_count == 0

        relocation = Relocation.objects.get(uuid=self.uuid)
        assert relocation.status == Relocation.Status.FAILURE.value
        assert relocation.failure_reason == ERR_PREPROCESSING_INTERNAL


@region_silo_test(stable=True)
@patch(
    "sentry.backup.helpers.KeyManagementServiceClient",
    new_callable=lambda: FakeKeyManagementServiceClient,
)
@patch("sentry.utils.relocation.MessageBuilder")
@patch("sentry.tasks.relocation.preprocessing_complete.delay")
class PreprocessingCollidingUsersTest(RelocationTaskTestCase):
    def setUp(self):
        super().setUp()
        self.relocation.step = Relocation.Step.PREPROCESSING.value
        self.relocation.latest_task = "PREPROCESSING_BASELINE_CONFIG"
        self.relocation.want_usernames = ["a", "b", "c"]
        self.relocation.save()

        self.create_user("c")
        self.create_user("d")
        self.create_user("e")

    def test_success(
        self,
        preprocessing_complete_mock: Mock,
        fake_message_builder: Mock,
        fake_kms_client: FakeKeyManagementServiceClient,
    ):
        self.mock_message_builder(fake_message_builder)
        self.mock_kms_client(fake_kms_client)

        preprocessing_colliding_users(self.uuid)

        assert fake_kms_client.asymmetric_decrypt.call_count == 0
        assert fake_kms_client.get_public_key.call_count == 1
        assert fake_message_builder.call_count == 0
        assert preprocessing_complete_mock.call_count == 1

        relocation_file = (
            RelocationFile.objects.filter(
                relocation=self.relocation,
                kind=RelocationFile.Kind.COLLIDING_USERS_VALIDATION_DATA.value,
            )
            .select_related("file")
            .first()
        )
        assert relocation_file.file.name == "colliding-users.tar"

        with relocation_file.file.getfile() as fp:
            json_models = json.loads(
                decrypt_encrypted_tarball(fp, LocalFileDecryptor.from_bytes(self.priv_key_pem))
            )
        assert len(json_models) > 0

        # Only user `c` was colliding, so only they should be exported.
        for json_model in json_models:
            if NormalizedModelName(json_model["model"]) == get_model_name(User):
                assert json_model["fields"]["username"] == "c"

    def test_retry_if_attempts_left(
        self,
        preprocessing_complete_mock: Mock,
        fake_message_builder: Mock,
        fake_kms_client: FakeKeyManagementServiceClient,
    ):
        RelocationFile.objects.filter(relocation=self.relocation).delete()
        self.mock_message_builder(fake_message_builder)
        self.mock_kms_client(fake_kms_client)

        # An exception being raised will trigger a retry in celery.
        with pytest.raises(Exception):
            fake_kms_client.get_public_key.side_effect = Exception("Test")

            preprocessing_colliding_users(self.uuid)

        assert fake_kms_client.asymmetric_decrypt.call_count == 0
        assert fake_kms_client.get_public_key.call_count == 1
        assert fake_message_builder.call_count == 0
        assert preprocessing_complete_mock.call_count == 0

        relocation = Relocation.objects.get(uuid=self.uuid)
        assert relocation.status == Relocation.Status.IN_PROGRESS.value
        assert not relocation.failure_reason

    def test_fail_if_no_attempts_left(
        self,
        preprocessing_complete_mock: Mock,
        fake_message_builder: Mock,
        fake_kms_client: FakeKeyManagementServiceClient,
    ):
        self.relocation.latest_task = "PREPROCESSING_COLLIDING_USERS"
        self.relocation.latest_task_attempts = MAX_FAST_TASK_RETRIES
        self.relocation.save()
        RelocationFile.objects.filter(relocation=self.relocation).delete()

        self.mock_message_builder(fake_message_builder)
        self.mock_kms_client(fake_kms_client)
        fake_kms_client.get_public_key.side_effect = Exception("Test")

        with pytest.raises(Exception):
            preprocessing_colliding_users(self.uuid)

        assert fake_kms_client.asymmetric_decrypt.call_count == 0
        assert fake_kms_client.get_public_key.call_count == 1

        assert fake_message_builder.call_count == 1
        assert fake_message_builder.call_args.kwargs["type"] == "relocation.failed"
        fake_message_builder.return_value.send_async.assert_called_once_with(
            to=[self.owner.email, self.superuser.email]
        )

        assert preprocessing_complete_mock.call_count == 0

        relocation = Relocation.objects.get(uuid=self.uuid)
        assert relocation.failure_reason == ERR_PREPROCESSING_INTERNAL
        assert relocation.status == Relocation.Status.FAILURE.value


@region_silo_test
@patch("sentry.utils.relocation.MessageBuilder")
@patch("sentry.tasks.relocation.validating_start.delay")
class PreprocessingCompleteTest(RelocationTaskTestCase):
    def setUp(self):
        super().setUp()
        self.relocation.step = Relocation.Step.PREPROCESSING.value
        self.relocation.latest_task = "PREPROCESSING_COLLIDING_USERS"
        self.relocation.want_usernames = ["importing"]
        self.relocation.save()
        self.create_user("importing")
        self.storage = get_storage()

        file = File.objects.create(name="baseline-config.tar", type=RELOCATION_FILE_TYPE)
        self.swap_file(file, "single-option.json", blob_size=16384)  # No chunking
        RelocationFile.objects.create(
            relocation=self.relocation,
            file=file,
            kind=RelocationFile.Kind.BASELINE_CONFIG_VALIDATION_DATA.value,
        )
        assert file.blobs.count() == 1  # So small that chunking is unnecessary.

        file = File.objects.create(name="colliding-users.tar", type=RELOCATION_FILE_TYPE)
        self.swap_file(file, "user-with-maximum-privileges.json", blob_size=8192)  # Forces chunks
        RelocationFile.objects.create(
            relocation=self.relocation,
            file=file,
            kind=RelocationFile.Kind.COLLIDING_USERS_VALIDATION_DATA.value,
        )
        assert file.blobs.count() > 1  # A bit bigger, so we get chunks.

    def test_success(
        self,
        validating_start_mock: Mock,
        fake_message_builder: Mock,
    ):
        self.mock_message_builder(fake_message_builder)
        assert not self.storage.exists(f"relocations/runs/{self.uuid}")

        preprocessing_complete(self.uuid)

        assert fake_message_builder.call_count == 0
        assert validating_start_mock.call_count == 1

        (_, files) = self.storage.listdir(f"relocations/runs/{self.uuid}/conf")
        assert len(files) == 2
        assert "cloudbuild.yaml" in files
        assert "cloudbuild.zip" in files

        cb_yaml_file = self.storage.open(f"relocations/runs/{self.uuid}/conf/cloudbuild.yaml")
        with cb_yaml_file:
            cb_conf = yaml.safe_load(cb_yaml_file)
            assert cb_conf is not None

        # These entries in the generated `cloudbuild.yaml` depend on the UUID, so check them
        # separately then replace them for snapshotting.
        in_path = cb_conf["steps"][0]["args"][2]
        findings_path = cb_conf["artifacts"]["objects"]["location"]
        assert in_path == f"gs://default/relocations/runs/{self.uuid}/in"
        assert findings_path == f"gs://default/relocations/runs/{self.uuid}/findings/"

        # Do a snapshot test of the cloudbuild config.
        cb_conf["steps"][0]["args"][2] = "gs://<BUCKET>/relocations/runs/<UUID>/in"
        cb_conf["artifacts"]["objects"][
            "location"
        ] = "gs://<BUCKET>/relocations/runs/<UUID>/findings/"
        cb_conf["steps"][12]["args"][3] = "gs://<BUCKET>/relocations/runs/<UUID>/out"
        self.insta_snapshot(cb_conf)

        (_, files) = self.storage.listdir(f"relocations/runs/{self.uuid}/in")
        assert len(files) == 4
        assert "kms-config.json" in files
        assert "raw-relocation-data.tar" in files
        assert "baseline-config.tar" in files
        assert "colliding-users.tar" in files

        kms_file = self.storage.open(f"relocations/runs/{self.uuid}/in/kms-config.json")
        with kms_file:
            json.load(kms_file)

        self.relocation.refresh_from_db()
        assert self.relocation.step == Relocation.Step.VALIDATING.value
        assert RelocationValidation.objects.filter(relocation=self.relocation).count() == 1

    def test_retry_if_attempts_left(
        self,
        validating_start_mock: Mock,
        fake_message_builder: Mock,
    ):
        RelocationFile.objects.filter(relocation=self.relocation).delete()
        self.mock_message_builder(fake_message_builder)

        # An exception being raised will trigger a retry in celery.
        with pytest.raises(Exception):
            preprocessing_complete(self.uuid)

        assert fake_message_builder.call_count == 0
        assert validating_start_mock.call_count == 0

        relocation = Relocation.objects.get(uuid=self.uuid)
        assert relocation.status == Relocation.Status.IN_PROGRESS.value
        assert not relocation.failure_reason

    def test_fail_if_no_attempts_left(
        self,
        validating_start_mock: Mock,
        fake_message_builder: Mock,
    ):
        self.relocation.latest_task = "PREPROCESSING_COMPLETE"
        self.relocation.latest_task_attempts = MAX_FAST_TASK_RETRIES
        self.relocation.save()
        RelocationFile.objects.filter(relocation=self.relocation).delete()
        self.mock_message_builder(fake_message_builder)

        with pytest.raises(Exception):
            preprocessing_complete(self.uuid)

        assert fake_message_builder.call_count == 1
        assert fake_message_builder.call_args.kwargs["type"] == "relocation.failed"
        fake_message_builder.return_value.send_async.assert_called_once_with(
            to=[self.owner.email, self.superuser.email]
        )

        assert validating_start_mock.call_count == 0

        relocation = Relocation.objects.get(uuid=self.uuid)
        assert relocation.status == Relocation.Status.FAILURE.value
        assert relocation.failure_reason == ERR_PREPROCESSING_INTERNAL


@region_silo_test(stable=True)
@patch(
    "sentry.tasks.relocation.CloudBuildClient",
    new_callable=lambda: FakeCloudBuildClient,
)
@patch("sentry.utils.relocation.MessageBuilder")
@patch("sentry.tasks.relocation.validating_poll.delay")
class ValidatingStartTest(RelocationTaskTestCase):
    def setUp(self):
        super().setUp()
        self.relocation.step = Relocation.Step.VALIDATING.value
        self.relocation.latest_task = "PREPROCESSING_COMPLETE"
        self.relocation.want_usernames = ["testuser"]
        self.relocation.want_org_slugs = ["test-slug"]
        self.relocation.save()

        self.relocation_validation: RelocationValidation = RelocationValidation.objects.create(
            relocation=self.relocation
        )

    def test_success(
        self,
        validating_poll_mock: Mock,
        fake_message_builder: Mock,
        fake_cloudbuild_client: FakeCloudBuildClient,
    ):
        self.mock_cloudbuild_client(fake_cloudbuild_client, Build.Status(Build.Status.QUEUED))
        self.mock_message_builder(fake_message_builder)

        validating_start(self.uuid)

        assert validating_poll_mock.call_count == 1
        assert fake_cloudbuild_client.create_build.call_count == 1

        self.relocation.refresh_from_db()
        self.relocation_validation.refresh_from_db()
        assert self.relocation_validation.status == ValidationStatus.IN_PROGRESS.value
        assert self.relocation_validation.attempts == 1

        relocation_validation_attempt = RelocationValidationAttempt.objects.get(
            relocation_validation=self.relocation_validation
        )
        assert relocation_validation_attempt.status == ValidationStatus.IN_PROGRESS.value

    def test_retry_if_attempts_left(
        self,
        validating_poll_mock: Mock,
        fake_message_builder: Mock,
        fake_cloudbuild_client: FakeCloudBuildClient,
    ):
        self.mock_cloudbuild_client(fake_cloudbuild_client, Build.Status(Build.Status.QUEUED))
        self.mock_message_builder(fake_message_builder)

        # An exception being raised will trigger a retry in celery.
        with pytest.raises(Exception):
            fake_cloudbuild_client.create_build.side_effect = Exception("Test")

            validating_start(self.uuid)

        assert fake_cloudbuild_client.create_build.call_count == 1
        assert fake_message_builder.call_count == 0
        assert validating_poll_mock.call_count == 0

        relocation = Relocation.objects.get(uuid=self.uuid)
        assert relocation.status == Relocation.Status.IN_PROGRESS.value
        assert not relocation.failure_reason

    def test_fail_if_no_attempts_left(
        self,
        validating_poll_mock: Mock,
        fake_message_builder: Mock,
        fake_cloudbuild_client: FakeCloudBuildClient,
    ):
        self.relocation.latest_task = "VALIDATING_START"
        self.relocation.latest_task_attempts = MAX_FAST_TASK_RETRIES
        self.relocation.save()

        self.mock_cloudbuild_client(fake_cloudbuild_client, Build.Status(Build.Status.QUEUED))
        fake_cloudbuild_client.create_build.side_effect = Exception("Test")
        self.mock_message_builder(fake_message_builder)

        with pytest.raises(Exception):
            validating_start(self.uuid)

        assert fake_cloudbuild_client.create_build.call_count == 1
        assert fake_message_builder.call_count == 1
        assert validating_poll_mock.call_count == 0

        relocation = Relocation.objects.get(uuid=self.uuid)
        assert relocation.status == Relocation.Status.FAILURE.value
        assert relocation.failure_reason == ERR_VALIDATING_INTERNAL

    def test_fail_if_max_runs_attempted(
        self,
        validating_poll_mock: Mock,
        fake_message_builder: Mock,
        fake_cloudbuild_client: FakeCloudBuildClient,
    ):
        for _ in range(3):
            RelocationValidationAttempt.objects.create(
                relocation=self.relocation,
                relocation_validation=self.relocation_validation,
                build_id=uuid4().hex,
            )

        self.relocation_validation.attempts = 3
        self.relocation_validation.save()

        self.relocation.latest_task_attempts = MAX_FAST_TASK_RETRIES
        self.relocation.save()

        self.mock_cloudbuild_client(fake_cloudbuild_client, Build.Status(Build.Status.QUEUED))
        self.mock_message_builder(fake_message_builder)

        validating_start(self.uuid)

        assert fake_cloudbuild_client.create_build.call_count == 0
        assert fake_message_builder.call_count == 1
        assert validating_poll_mock.call_count == 0

        relocation = Relocation.objects.get(uuid=self.uuid)
        assert relocation.status == Relocation.Status.FAILURE.value
        assert relocation.failure_reason == ERR_VALIDATING_MAX_RUNS


@region_silo_test(stable=True)
@patch(
    "sentry.tasks.relocation.CloudBuildClient",
    new_callable=lambda: FakeCloudBuildClient,
)
@patch("sentry.utils.relocation.MessageBuilder")
class ValidatingPollTest(RelocationTaskTestCase):
    def setUp(self):
        super().setUp()
        self.relocation.step = Relocation.Step.VALIDATING.value
        self.relocation.latest_task = "VALIDATING_START"
        self.relocation.want_usernames = ["testuser"]
        self.relocation.want_org_slugs = ["test-slug"]
        self.relocation.save()

        self.relocation_validation: RelocationValidation = RelocationValidation.objects.create(
            relocation=self.relocation, attempts=1
        )

        self.relocation_validation_attempt: RelocationValidationAttempt = (
            RelocationValidationAttempt.objects.create(
                relocation=self.relocation,
                relocation_validation=self.relocation_validation,
                build_id=uuid4().hex,
            )
        )

    @patch("sentry.tasks.relocation.validating_complete.delay")
    def test_success(
        self,
        validating_complete_mock: Mock,
        fake_message_builder: Mock,
        fake_cloudbuild_client: FakeCloudBuildClient,
    ):
        self.mock_cloudbuild_client(fake_cloudbuild_client, Build.Status(Build.Status.SUCCESS))
        self.mock_message_builder(fake_message_builder)

        validating_poll(self.uuid, self.relocation_validation_attempt.build_id)

        assert fake_cloudbuild_client.get_build.call_count == 1
        assert fake_message_builder.call_count == 0
        assert validating_complete_mock.call_count == 1

        self.relocation.refresh_from_db()
        self.relocation_validation.refresh_from_db()
        self.relocation_validation_attempt.refresh_from_db()
        assert self.relocation_validation.status == ValidationStatus.IN_PROGRESS.value
        assert self.relocation.latest_task == "VALIDATING_POLL"

    @patch("sentry.tasks.relocation.validating_start.delay")
    def test_timeout_starts_new_validation_attempt(
        self,
        validating_start_mock: Mock,
        fake_message_builder: Mock,
        fake_cloudbuild_client: FakeCloudBuildClient,
    ):
        for stat in {Build.Status.TIMEOUT, Build.Status.EXPIRED}:
            self.mock_message_builder(fake_message_builder)
            self.mock_cloudbuild_client(fake_cloudbuild_client, Build.Status(stat))
            validating_start_mock.call_count = 0

            validating_poll(self.uuid, self.relocation_validation_attempt.build_id)

            assert fake_cloudbuild_client.get_build.call_count == 1
            assert fake_message_builder.call_count == 0
            assert validating_start_mock.call_count == 1

            self.relocation.refresh_from_db()
            self.relocation_validation.refresh_from_db()
            self.relocation_validation_attempt.refresh_from_db()

            assert self.relocation.latest_task == "VALIDATING_START"
            assert self.relocation_validation.status == ValidationStatus.IN_PROGRESS.value
            assert self.relocation_validation_attempt.status == ValidationStatus.TIMEOUT.value

    @patch("sentry.tasks.relocation.validating_start.delay")
    def test_failure_starts_new_validation_attempt(
        self,
        validating_start_mock: Mock,
        fake_message_builder: Mock,
        fake_cloudbuild_client: FakeCloudBuildClient,
    ):
        for stat in {
            Build.Status.FAILURE,
            Build.Status.INTERNAL_ERROR,
            Build.Status.CANCELLED,
        }:
            self.mock_cloudbuild_client(fake_cloudbuild_client, Build.Status(stat))
            self.mock_message_builder(fake_message_builder)
            validating_start_mock.call_count = 0

            validating_poll(self.uuid, self.relocation_validation_attempt.build_id)

            assert fake_cloudbuild_client.get_build.call_count == 1
            assert fake_message_builder.call_count == 0
            assert validating_start_mock.call_count == 1

            self.relocation.refresh_from_db()
            self.relocation_validation.refresh_from_db()
            self.relocation_validation_attempt.refresh_from_db()
            assert self.relocation.latest_task == "VALIDATING_START"
            assert self.relocation_validation.status == ValidationStatus.IN_PROGRESS.value
            assert self.relocation_validation_attempt.status == ValidationStatus.FAILURE.value

    @patch("sentry.tasks.relocation.validating_poll.apply_async")
    def test_in_progress_retries_poll(
        self,
        validating_poll_mock: Mock,
        fake_message_builder: Mock,
        fake_cloudbuild_client: FakeCloudBuildClient,
    ):
        for stat in {
            Build.Status.QUEUED,
            Build.Status.PENDING,
            Build.Status.WORKING,
        }:
            self.mock_cloudbuild_client(fake_cloudbuild_client, Build.Status(stat))
            self.mock_message_builder(fake_message_builder)
            validating_poll_mock.call_count = 0

            validating_poll(self.uuid, self.relocation_validation_attempt.build_id)

            assert fake_cloudbuild_client.get_build.call_count == 1
            assert fake_message_builder.call_count == 0
            assert validating_poll_mock.call_count == 1

            self.relocation.refresh_from_db()
            self.relocation_validation.refresh_from_db()
            self.relocation_validation_attempt.refresh_from_db()
            assert self.relocation.latest_task == "VALIDATING_POLL"
            assert self.relocation_validation.status == ValidationStatus.IN_PROGRESS.value
            assert self.relocation_validation_attempt.status == ValidationStatus.IN_PROGRESS.value
            assert (
                RelocationValidationAttempt.objects.filter(
                    relocation_validation=self.relocation_validation
                ).count()
                == 1
            )

    @patch("sentry.tasks.relocation.validating_poll.apply_async")
    def test_retry_if_attempts_left(
        self,
        validating_poll_mock: Mock,
        fake_message_builder: Mock,
        fake_cloudbuild_client: FakeCloudBuildClient,
    ):
        self.mock_cloudbuild_client(fake_cloudbuild_client, Build.Status(Build.Status.QUEUED))
        self.mock_message_builder(fake_message_builder)
        fake_cloudbuild_client.get_build.side_effect = Exception("Test")

        # An exception being raised will trigger a retry in celery.
        with pytest.raises(Exception):
            validating_poll(self.uuid, self.relocation_validation_attempt.build_id)

        assert fake_cloudbuild_client.get_build.call_count == 1
        assert fake_message_builder.call_count == 0
        assert validating_poll_mock.call_count == 0

        relocation = Relocation.objects.get(uuid=self.uuid)
        assert relocation.status == Relocation.Status.IN_PROGRESS.value
        assert not relocation.failure_reason

    @patch("sentry.tasks.relocation.validating_poll.apply_async")
    def test_fail_if_no_attempts_left(
        self,
        validating_poll_mock: Mock,
        fake_message_builder: Mock,
        fake_cloudbuild_client: FakeCloudBuildClient,
    ):
        self.relocation.latest_task = "VALIDATING_POLL"
        self.relocation.latest_task_attempts = MAX_VALIDATION_POLLS
        self.relocation.save()

        self.mock_cloudbuild_client(fake_cloudbuild_client, Build.Status(Build.Status.QUEUED))
        fake_cloudbuild_client.get_build.side_effect = Exception("Test")
        self.mock_message_builder(fake_message_builder)

        with pytest.raises(Exception):
            validating_poll(self.uuid, self.relocation_validation_attempt.build_id)

        assert fake_cloudbuild_client.get_build.call_count == 1

        assert fake_message_builder.call_count == 1
        assert fake_message_builder.call_args.kwargs["type"] == "relocation.failed"
        fake_message_builder.return_value.send_async.assert_called_once_with(
            to=[self.owner.email, self.superuser.email]
        )

        assert validating_poll_mock.call_count == 0

        relocation = Relocation.objects.get(uuid=self.uuid)
        assert relocation.status == Relocation.Status.FAILURE.value
        assert relocation.failure_reason == ERR_VALIDATING_INTERNAL


def mock_invalid_finding(storage: Storage, uuid: str):
    storage.save(
        f"relocations/runs/{uuid}/findings/import-baseline-config.json",
        BytesIO(
            b"""
[
    {
        "finding": "RpcImportError",
        "kind": "Unknown",
        "left_pk": 2,
        "on": {
            "model": "sentry.email",
            "ordinal": 1
        },
        "reason": "test reason",
        "right_pk": 3
    }
]
            """
        ),
    )


@region_silo_test(stable=True)
@patch("sentry.utils.relocation.MessageBuilder")
@patch("sentry.tasks.relocation.importing.delay")
class ValidatingCompleteTest(RelocationTaskTestCase):
    def setUp(self):
        super().setUp()
        self.relocation.step = Relocation.Step.VALIDATING.value
        self.relocation.latest_task = "VALIDATING_POLL"
        self.relocation.want_usernames = ["testuser"]
        self.relocation.want_org_slugs = ["test-slug"]
        self.relocation.save()

        self.relocation_validation: RelocationValidation = RelocationValidation.objects.create(
            relocation=self.relocation, attempts=1
        )

        self.relocation_validation_attempt: RelocationValidationAttempt = (
            RelocationValidationAttempt.objects.create(
                relocation=self.relocation,
                relocation_validation=self.relocation_validation,
                build_id=uuid4().hex,
            )
        )

        self.storage = get_storage()
        self.storage.save(
            f"relocations/runs/{self.uuid}/findings/artifacts-prefixes-are-ignored.json",
            BytesIO(b"invalid-json"),
        )
        files = [
            "null.json",
            "import-baseline-config.json",
            "import-colliding-users.json",
            "import-raw-relocation-data.json",
            "export-baseline-config.json",
            "export-colliding-users.json",
            "export-raw-relocation-data.json",
            "compare-baseline-config.json",
            "compare-colliding-users.json",
        ]
        for file in files:
            self.storage.save(f"relocations/runs/{self.uuid}/findings/{file}", BytesIO(b"[]"))

    def test_valid(
        self,
        importing_mock: Mock,
        fake_message_builder: Mock,
    ):
        self.mock_message_builder(fake_message_builder)

        validating_complete(self.uuid, self.relocation_validation_attempt.build_id)

        assert fake_message_builder.call_count == 0
        assert importing_mock.call_count == 1

        self.relocation.refresh_from_db()
        self.relocation_validation.refresh_from_db()
        self.relocation_validation_attempt.refresh_from_db()
        assert self.relocation.latest_task == "VALIDATING_COMPLETE"
        assert self.relocation.step == Relocation.Step.IMPORTING.value
        assert self.relocation_validation.status == ValidationStatus.VALID.value
        assert self.relocation_validation_attempt.status == ValidationStatus.VALID.value

    def test_invalid(
        self,
        importing_mock: Mock,
        fake_message_builder: Mock,
    ):
        self.mock_message_builder(fake_message_builder)
        mock_invalid_finding(self.storage, self.uuid)

        validating_complete(self.uuid, self.relocation_validation_attempt.build_id)

        assert fake_message_builder.call_count == 1
        assert fake_message_builder.call_args.kwargs["type"] == "relocation.failed"
        fake_message_builder.return_value.send_async.assert_called_once_with(
            to=[self.owner.email, self.superuser.email]
        )

        assert importing_mock.call_count == 0

        self.relocation.refresh_from_db()
        self.relocation_validation.refresh_from_db()
        self.relocation_validation_attempt.refresh_from_db()
        assert self.relocation.latest_task == "VALIDATING_COMPLETE"
        assert self.relocation.step == Relocation.Step.VALIDATING.value
        assert self.relocation.failure_reason is not None
        assert self.relocation_validation.status == ValidationStatus.INVALID.value
        assert self.relocation_validation_attempt.status == ValidationStatus.INVALID.value

    def test_retry_if_attempts_left(
        self,
        importing_mock: Mock,
        fake_message_builder: Mock,
    ):
        self.mock_message_builder(fake_message_builder)
        self.storage.save(
            f"relocations/runs/{self.uuid}/findings/null.json",
            BytesIO(b"invalid-json"),
        )

        assert fake_message_builder.call_count == 0
        assert importing_mock.call_count == 0

        # An exception being raised will trigger a retry in celery.
        with pytest.raises(Exception):
            validating_complete(self.uuid, self.relocation_validation_attempt.build_id)

        relocation = Relocation.objects.get(uuid=self.uuid)
        assert relocation.status == Relocation.Status.IN_PROGRESS.value
        assert not relocation.failure_reason

    def test_fail_if_no_attempts_left(
        self,
        importing_mock: Mock,
        fake_message_builder: Mock,
    ):
        self.mock_message_builder(fake_message_builder)
        self.relocation.latest_task = "VALIDATING_COMPLETE"
        self.relocation.latest_task_attempts = MAX_FAST_TASK_RETRIES
        self.relocation.save()
        self.storage.save(
            f"relocations/runs/{self.uuid}/findings/null.json", BytesIO(b"invalid-json")
        )

        with pytest.raises(Exception):
            validating_complete(self.uuid, self.relocation_validation_attempt.build_id)

        assert fake_message_builder.call_count == 1
        assert fake_message_builder.call_args.kwargs["type"] == "relocation.failed"
        fake_message_builder.return_value.send_async.assert_called_once_with(
            to=[self.owner.email, self.superuser.email]
        )

        assert importing_mock.call_count == 0

        relocation = Relocation.objects.get(uuid=self.uuid)
        assert relocation.status == Relocation.Status.FAILURE.value
        assert relocation.failure_reason == ERR_VALIDATING_INTERNAL


@region_silo_test(stable=True)
@patch(
    "sentry.backup.helpers.KeyManagementServiceClient",
    new_callable=lambda: FakeKeyManagementServiceClient,
)
@patch("sentry.tasks.relocation.postprocessing.delay")
class ImportingTest(RelocationTaskTestCase, TransactionTestCase):
    def setUp(self):
        RelocationTaskTestCase.setUp(self)
        TransactionTestCase.setUp(self)
        self.relocation.step = Relocation.Step.VALIDATING.value
        self.relocation.latest_task = "VALIDATING_COMPLETE"
        self.relocation.save()

    def test_success(
        self, postprocessing_mock: Mock, fake_kms_client: FakeKeyManagementServiceClient
    ):
        self.mock_kms_client(fake_kms_client)
        org_count = Organization.objects.filter(slug__startswith="testing").count()

        importing(self.uuid)

        assert postprocessing_mock.call_count == 1
        assert Organization.objects.filter(slug__startswith="testing").count() == org_count + 1

        assert RegionImportChunk.objects.filter(import_uuid=self.uuid).count() == 9
        assert sorted(RegionImportChunk.objects.values_list("model", flat=True)) == [
            "sentry.organization",
            "sentry.organizationmember",
            "sentry.organizationmemberteam",
            "sentry.project",
            "sentry.projectkey",
            "sentry.projectoption",
            "sentry.projectteam",
            "sentry.rule",
            "sentry.team",
        ]

        with assume_test_silo_mode(SiloMode.CONTROL):
            assert ControlImportChunk.objects.filter(import_uuid=self.uuid).count() == 2
            assert sorted(ControlImportChunk.objects.values_list("model", flat=True)) == [
                "sentry.user",
                "sentry.useremail",
            ]


@region_silo_test(stable=True)
@patch("sentry.utils.relocation.MessageBuilder")
@patch("sentry.tasks.relocation.notifying_users.delay")
class PostprocessingTest(RelocationTaskTestCase):
    def setUp(self):
        RelocationTaskTestCase.setUp(self)
        TransactionTestCase.setUp(self)
        self.relocation.step = Relocation.Step.IMPORTING.value
        self.relocation.latest_task = "IMPORTING"
        self.relocation.save()

        with open(IMPORT_JSON_FILE_PATH, "rb") as fp:
            import_in_organization_scope(
                fp,
                flags=ImportFlags(
                    merge_users=False, overwrite_configs=False, import_uuid=str(self.uuid)
                ),
                org_filter=set(self.relocation.want_org_slugs),
            )

        imported_orgs = RegionImportChunk.objects.get(
            import_uuid=self.uuid, model="sentry.organization"
        )
        assert len(imported_orgs.inserted_map) == 1
        assert len(imported_orgs.inserted_identifiers) == 1

        self.imported_org_id: int = next(iter(imported_orgs.inserted_map.values()))
        self.imported_org_slug: str = next(iter(imported_orgs.inserted_identifiers.values()))

    def test_success(
        self,
        notifying_users_mock: Mock,
        fake_message_builder: Mock,
    ):
        self.mock_message_builder(fake_message_builder)
        assert (
            OrganizationMember.objects.filter(
                organization_id=self.imported_org_id, role="owner", has_global_access=True
            ).count()
            == 1
        )
        assert not OrganizationMember.objects.filter(
            organization_id=self.imported_org_id, user_id=self.owner.id
        ).exists()

        postprocessing(self.uuid)

        assert notifying_users_mock.call_count == 1

        assert (
            OrganizationMember.objects.filter(
                organization_id=self.imported_org_id, role="owner", has_global_access=True
            ).count()
            == 2
        )
        assert OrganizationMember.objects.filter(
            organization_id=self.imported_org_id, user_id=self.owner.id
        ).exists()

    def test_retry_if_attempts_left(
        self,
        notifying_users_mock: Mock,
        fake_message_builder: Mock,
    ):
        self.mock_message_builder(fake_message_builder)
        self.relocation.want_org_slugs = ["incorrect-slug"]
        self.relocation.save()

        # An exception being raised will trigger a retry in celery.
        with pytest.raises(Exception):
            postprocessing(self.uuid)

        assert fake_message_builder.call_count == 0
        assert notifying_users_mock.call_count == 0

        relocation = Relocation.objects.get(uuid=self.uuid)
        assert relocation.status == Relocation.Status.IN_PROGRESS.value
        assert not relocation.failure_reason

    def test_fail_if_no_attempts_left(
        self,
        notifying_users_mock: Mock,
        fake_message_builder: Mock,
    ):
        self.mock_message_builder(fake_message_builder)
        self.relocation.latest_task = "POSTPROCESSING"
        self.relocation.latest_task_attempts = MAX_FAST_TASK_RETRIES
        self.relocation.want_org_slugs = ["incorrect-slug"]
        self.relocation.save()

        with pytest.raises(Exception):
            postprocessing(self.uuid)

        assert fake_message_builder.call_count == 1
        assert fake_message_builder.call_args.kwargs["type"] == "relocation.failed"
        fake_message_builder.return_value.send_async.assert_called_once_with(
            to=[self.owner.email, self.superuser.email]
        )

        assert notifying_users_mock.call_count == 0

        relocation = Relocation.objects.get(uuid=self.uuid)
        assert relocation.status == Relocation.Status.FAILURE.value
        assert relocation.failure_reason == ERR_POSTPROCESSING_INTERNAL


@region_silo_test(stable=True)
@patch("sentry.utils.relocation.MessageBuilder")
@patch("sentry.tasks.relocation.notifying_owner.delay")
class NotifyingUsersTest(RelocationTaskTestCase):
    def setUp(self):
        RelocationTaskTestCase.setUp(self)
        TransactionTestCase.setUp(self)
        self.relocation.step = Relocation.Step.POSTPROCESSING.value
        self.relocation.latest_task = "POSTPROCESSING"
        self.relocation.want_usernames = ["admin@example.com", "member@example.com"]
        self.relocation.save()

        with open(IMPORT_JSON_FILE_PATH, "rb") as fp:
            import_in_organization_scope(
                fp,
                flags=ImportFlags(
                    merge_users=False, overwrite_configs=False, import_uuid=str(self.uuid)
                ),
                org_filter=set(self.relocation.want_org_slugs),
            )

        self.imported_users = ControlImportChunkReplica.objects.get(
            import_uuid=self.uuid, model="sentry.user"
        )

        assert len(self.imported_users.inserted_map) == 2

    def test_success(
        self,
        notifying_owner_mock: Mock,
        fake_message_builder: Mock,
    ):
        self.mock_message_builder(fake_message_builder)

        with patch.object(LostPasswordHash, "send_relocate_account_email") as mock_relocation_email:
            notifying_users(self.uuid)

            # Called once for each user imported, which is 2 for `fresh-install.json`
            assert mock_relocation_email.call_count == 2
            email_targets = [
                mock_relocation_email.call_args_list[0][0][0].username,
                mock_relocation_email.call_args_list[1][0][0].username,
            ]
            assert "admin@example.com" in email_targets
            assert "member@example.com" in email_targets

            assert fake_message_builder.call_count == 0
            assert notifying_owner_mock.call_count == 1

    def test_retry_if_attempts_left(
        self,
        notifying_owner_mock: Mock,
        fake_message_builder: Mock,
    ):
        self.mock_message_builder(fake_message_builder)
        self.relocation.want_usernames = ["doesnotexist"]
        self.relocation.save()

        # An exception being raised will trigger a retry in celery.
        with pytest.raises(Exception):
            notifying_users(self.uuid)

        assert fake_message_builder.call_count == 0
        assert notifying_owner_mock.call_count == 0

        relocation = Relocation.objects.get(uuid=self.uuid)
        assert relocation.status == Relocation.Status.IN_PROGRESS.value
        assert not relocation.failure_reason

    def test_fail_if_no_attempts_left(
        self,
        notifying_owner_mock: Mock,
        fake_message_builder: Mock,
    ):
        self.mock_message_builder(fake_message_builder)
        self.relocation.latest_task = "NOTIFYING_USERS"
        self.relocation.latest_task_attempts = MAX_FAST_TASK_RETRIES
        self.relocation.want_usernames = ["doesnotexist"]
        self.relocation.save()

        with pytest.raises(Exception):
            notifying_users(self.uuid)

        assert fake_message_builder.call_count == 1
        assert fake_message_builder.call_args.kwargs["type"] == "relocation.failed"
        fake_message_builder.return_value.send_async.assert_called_once_with(
            to=[self.owner.email, self.superuser.email]
        )
        assert notifying_owner_mock.call_count == 0

        relocation = Relocation.objects.get(uuid=self.uuid)
        assert relocation.status == Relocation.Status.FAILURE.value
        assert relocation.failure_reason == ERR_NOTIFYING_INTERNAL


@region_silo_test(stable=True)
@patch("sentry.utils.relocation.MessageBuilder")
@patch("sentry.tasks.relocation.completed.delay")
class NotifyingOwnerTest(RelocationTaskTestCase):
    def setUp(self):
        RelocationTaskTestCase.setUp(self)
        TransactionTestCase.setUp(self)
        self.relocation.step = Relocation.Step.NOTIFYING.value
        self.relocation.latest_task = "NOTIFYING_USERS"
        self.relocation.save()

    def test_success_admin_assisted_relocation(
        self,
        completed_mock: Mock,
        fake_message_builder: Mock,
    ):
        self.mock_message_builder(fake_message_builder)

        notifying_owner(self.uuid)

        assert fake_message_builder.call_count == 1
        assert fake_message_builder.call_args.kwargs["type"] == "relocation.succeeded"
        fake_message_builder.return_value.send_async.assert_called_once_with(
            to=[self.owner.email, self.superuser.email]
        )

        assert completed_mock.call_count == 1

    def test_success_self_serve_relocation(
        self,
        completed_mock: Mock,
        fake_message_builder: Mock,
    ):
        self.mock_message_builder(fake_message_builder)
        self.relocation.creator_id = self.relocation.owner_id
        self.relocation.save()

        notifying_owner(self.uuid)

        assert fake_message_builder.call_count == 1
        assert fake_message_builder.call_args.kwargs["type"] == "relocation.succeeded"
        fake_message_builder.return_value.send_async.assert_called_once_with(to=[self.owner.email])

        assert completed_mock.call_count == 1

    def test_retry_if_attempts_left(
        self,
        completed_mock: Mock,
        fake_message_builder: Mock,
    ):
        self.mock_message_builder(fake_message_builder)
        fake_message_builder.return_value.send_async.side_effect = Exception("Test")

        # An exception being raised will trigger a retry in celery.
        with pytest.raises(Exception):
            notifying_owner(self.uuid)

        assert fake_message_builder.call_count == 1
        assert completed_mock.call_count == 0

        relocation = Relocation.objects.get(uuid=self.uuid)
        assert relocation.status == Relocation.Status.IN_PROGRESS.value
        assert not relocation.failure_reason

    def test_fail_if_no_attempts_left(
        self,
        completed_mock: Mock,
        fake_message_builder: Mock,
    ):
        self.relocation.latest_task = "NOTIFYING_OWNER"
        self.relocation.latest_task_attempts = MAX_FAST_TASK_RETRIES
        self.relocation.save()

        self.mock_message_builder(fake_message_builder)
        fake_message_builder.return_value.send_async.side_effect = [Exception("Test"), None]

        with pytest.raises(Exception):
            notifying_owner(self.uuid)

        # Oh, the irony: sending the "relocation success" email failed, so we send a "relocation
        # failed" email instead...
        assert fake_message_builder.call_count == 2
        email_types = [args.kwargs["type"] for args in fake_message_builder.call_args_list]
        assert "relocation.failed" in email_types
        assert "relocation.succeeded" in email_types

        assert completed_mock.call_count == 0

        relocation = Relocation.objects.get(uuid=self.uuid)
        assert relocation.status == Relocation.Status.FAILURE.value
        assert relocation.failure_reason == ERR_NOTIFYING_INTERNAL


@region_silo_test(stable=True)
class CompletedTest(RelocationTaskTestCase):
    def setUp(self):
        RelocationTaskTestCase.setUp(self)
        TransactionTestCase.setUp(self)
        self.relocation.step = Relocation.Step.NOTIFYING.value
        self.relocation.latest_task = "NOTIFYING_OWNER"
        self.relocation.save()

    def test_success(self):
        completed(self.uuid)

        relocation = Relocation.objects.get(uuid=self.uuid)
        assert relocation.status == Relocation.Status.SUCCESS.value
        assert not relocation.failure_reason


@region_silo_test(stable=True)
@patch(
    "sentry.backup.helpers.KeyManagementServiceClient",
    new_callable=lambda: FakeKeyManagementServiceClient,
)
@patch(
    "sentry.tasks.relocation.CloudBuildClient",
    new_callable=lambda: FakeCloudBuildClient,
)
@patch("sentry.utils.relocation.MessageBuilder")
class EndToEndTest(RelocationTaskTestCase, TransactionTestCase):
    def setUp(self):
        RelocationTaskTestCase.setUp(self)
        TransactionTestCase.setUp(self)

        self.storage = get_storage()
        files = [
            "null.json",
            "import-baseline-config.json",
            "import-colliding-users.json",
            "import-raw-relocation-data.json",
            "export-baseline-config.json",
            "export-colliding-users.json",
            "export-raw-relocation-data.json",
            "compare-baseline-config.json",
            "compare-colliding-users.json",
        ]
        for file in files:
            self.storage.save(
                f"relocations/runs/{self.relocation.uuid}/findings/{file}", BytesIO(b"[]")
            )

    def mock_max_retries(
        self,
        fake_cloudbuild_client: FakeCloudBuildClient,
        fake_kms_client: FakeKeyManagementServiceClient,
    ):
        fake_cloudbuild_client.create_build.side_effect = (
            [BustTaskRunnerRetryError("Retry")] * MAX_FAST_TASK_RETRIES
        ) + [fake_cloudbuild_client.create_build.return_value]

        fake_cloudbuild_client.get_build.side_effect = (
            [BustTaskRunnerRetryError("Retry")] * MAX_VALIDATION_POLLS
        ) + [fake_cloudbuild_client.get_build.return_value]

        fake_kms_client.asymmetric_decrypt.side_effect = (
            [BustTaskRunnerRetryError("Retry")] * MAX_FAST_TASK_RETRIES
        ) + [
            fake_kms_client.asymmetric_decrypt.return_value,
            # The second call to `asymmetric_decrypt` occurs from inside the `importing` task, which
            # is not retried.
            fake_kms_client.asymmetric_decrypt.return_value,
        ]

        fake_kms_client.get_public_key.side_effect = (
            [BustTaskRunnerRetryError("Retry")] * MAX_FAST_TASK_RETRIES
        ) + [fake_kms_client.get_public_key.return_value]
        # Used by two tasks, so repeat the pattern (fail, fail, fail, succeed) twice.
        fake_kms_client.get_public_key.side_effect = (
            list(fake_kms_client.get_public_key.side_effect) * 2
        )

    def assert_success_database_state(self, org_count: int):
        assert Organization.objects.filter(slug__startswith="testing").count() == org_count + 1

        assert RegionImportChunk.objects.filter(import_uuid=self.uuid).count() == 9
        assert sorted(RegionImportChunk.objects.values_list("model", flat=True)) == [
            "sentry.organization",
            "sentry.organizationmember",
            "sentry.organizationmemberteam",
            "sentry.project",
            "sentry.projectkey",
            "sentry.projectoption",
            "sentry.projectteam",
            "sentry.rule",
            "sentry.team",
        ]

        assert ControlImportChunkReplica.objects.filter(import_uuid=self.uuid).count() == 2
        with assume_test_silo_mode(SiloMode.CONTROL):
            assert ControlImportChunk.objects.filter(import_uuid=self.uuid).count() == 2
            assert sorted(ControlImportChunk.objects.values_list("model", flat=True)) == [
                "sentry.user",
                "sentry.useremail",
            ]

    def assert_failure_database_state(self, org_count: int):
        assert Organization.objects.filter(slug__startswith="testing").count() == org_count
        assert RegionImportChunk.objects.filter(import_uuid=self.uuid).count() == 0

        assert ControlImportChunkReplica.objects.filter(import_uuid=self.uuid).count() == 0
        with assume_test_silo_mode(SiloMode.CONTROL):
            assert ControlImportChunk.objects.filter(import_uuid=self.uuid).count() == 0

    def test_valid_no_retries(
        self,
        fake_message_builder: Mock,
        fake_cloudbuild_client: FakeCloudBuildClient,
        fake_kms_client: FakeKeyManagementServiceClient,
    ):
        self.mock_cloudbuild_client(fake_cloudbuild_client, Build.Status(Build.Status.SUCCESS))
        self.mock_kms_client(fake_kms_client)
        self.mock_message_builder(fake_message_builder)
        org_count = Organization.objects.filter(slug__startswith="testing").count()

        with BurstTaskRunner() as burst:
            uploading_complete(self.relocation.uuid)

        with patch.object(LostPasswordHash, "send_relocate_account_email") as mock_relocation_email:
            burst()

            assert mock_relocation_email.call_count == 2

        assert fake_cloudbuild_client.create_build.call_count == 1
        assert fake_cloudbuild_client.get_build.call_count == 1

        assert fake_kms_client.asymmetric_decrypt.call_count == 2
        assert fake_kms_client.get_public_key.call_count == 2

        assert fake_message_builder.call_count == 2
        email_types = [args.kwargs["type"] for args in fake_message_builder.call_args_list]
        assert "relocation.started" in email_types
        assert "relocation.succeeded" in email_types
        assert "relocation.failed" not in email_types

        relocation = Relocation.objects.get(uuid=self.uuid)
        assert relocation.status == Relocation.Status.SUCCESS.value
        assert not relocation.failure_reason

        self.assert_success_database_state(org_count)

    def test_valid_max_retries(
        self,
        fake_message_builder: Mock,
        fake_cloudbuild_client: FakeCloudBuildClient,
        fake_kms_client: FakeKeyManagementServiceClient,
    ):
        self.mock_cloudbuild_client(fake_cloudbuild_client, Build.Status(Build.Status.SUCCESS))
        self.mock_kms_client(fake_kms_client)
        self.mock_max_retries(fake_cloudbuild_client, fake_kms_client)

        self.mock_message_builder(fake_message_builder)
        org_count = Organization.objects.filter(slug__startswith="testing").count()

        with BurstTaskRunner() as burst:
            uploading_complete(self.relocation.uuid)

        with patch.object(LostPasswordHash, "send_relocate_account_email") as mock_relocation_email:
            burst()

            assert mock_relocation_email.call_count == 2

        assert fake_cloudbuild_client.create_build.call_count == MAX_FAST_TASK_ATTEMPTS
        assert fake_cloudbuild_client.get_build.call_count == MAX_VALIDATION_POLL_ATTEMPTS

        assert fake_kms_client.asymmetric_decrypt.call_count == MAX_FAST_TASK_ATTEMPTS + 1
        assert fake_kms_client.get_public_key.call_count == 2 * MAX_FAST_TASK_ATTEMPTS

        assert fake_message_builder.call_count == 2
        email_types = [args.kwargs["type"] for args in fake_message_builder.call_args_list]
        assert "relocation.started" in email_types
        assert "relocation.succeeded" in email_types
        assert "relocation.failed" not in email_types

        relocation = Relocation.objects.get(uuid=self.uuid)
        assert relocation.status == Relocation.Status.SUCCESS.value
        assert not relocation.failure_reason

        self.assert_success_database_state(org_count)

    def test_invalid_no_retries(
        self,
        fake_message_builder: Mock,
        fake_cloudbuild_client: FakeCloudBuildClient,
        fake_kms_client: FakeKeyManagementServiceClient,
    ):
        self.mock_cloudbuild_client(fake_cloudbuild_client, Build.Status(Build.Status.SUCCESS))
        self.mock_kms_client(fake_kms_client)
        self.mock_message_builder(fake_message_builder)
        mock_invalid_finding(self.storage, self.uuid)
        org_count = Organization.objects.filter(slug__startswith="testing").count()

        with BurstTaskRunner() as burst:
            uploading_complete(self.relocation.uuid)

        with patch.object(LostPasswordHash, "send_relocate_account_email") as mock_relocation_email:
            burst()

            assert mock_relocation_email.call_count == 0

        assert fake_cloudbuild_client.create_build.call_count == 1
        assert fake_cloudbuild_client.get_build.call_count == 1

        assert fake_kms_client.asymmetric_decrypt.call_count == 1
        assert fake_kms_client.get_public_key.call_count == 2

        assert fake_message_builder.call_count == 2
        email_types = [args.kwargs["type"] for args in fake_message_builder.call_args_list]
        assert "relocation.started" in email_types
        assert "relocation.failed" in email_types
        assert "relocation.succeeded" not in email_types

        relocation = Relocation.objects.get(uuid=self.uuid)
        assert relocation.status == Relocation.Status.FAILURE.value
        assert relocation.failure_reason

        self.assert_failure_database_state(org_count)

    def test_invalid_max_retries(
        self,
        fake_message_builder: Mock,
        fake_cloudbuild_client: FakeCloudBuildClient,
        fake_kms_client: FakeKeyManagementServiceClient,
    ):
        self.mock_cloudbuild_client(fake_cloudbuild_client, Build.Status(Build.Status.SUCCESS))
        self.mock_kms_client(fake_kms_client)
        self.mock_max_retries(fake_cloudbuild_client, fake_kms_client)

        self.mock_message_builder(fake_message_builder)
        mock_invalid_finding(self.storage, self.uuid)
        org_count = Organization.objects.filter(slug__startswith="testing").count()

        with BurstTaskRunner() as burst:
            uploading_complete(self.relocation.uuid)

        with patch.object(LostPasswordHash, "send_relocate_account_email") as mock_relocation_email:
            burst()

            assert mock_relocation_email.call_count == 0

        assert fake_cloudbuild_client.create_build.call_count == MAX_FAST_TASK_ATTEMPTS
        assert fake_cloudbuild_client.get_build.call_count == MAX_VALIDATION_POLL_ATTEMPTS

        assert fake_kms_client.asymmetric_decrypt.call_count == MAX_FAST_TASK_ATTEMPTS
        assert fake_kms_client.get_public_key.call_count == 2 * MAX_FAST_TASK_ATTEMPTS

        assert fake_message_builder.call_count == 2
        email_types = [args.kwargs["type"] for args in fake_message_builder.call_args_list]
        assert "relocation.started" in email_types
        assert "relocation.failed" in email_types
        assert "relocation.succeeded" not in email_types

        relocation = Relocation.objects.get(uuid=self.uuid)
        assert relocation.status == Relocation.Status.FAILURE.value
        assert relocation.failure_reason

        self.assert_failure_database_state(org_count)
