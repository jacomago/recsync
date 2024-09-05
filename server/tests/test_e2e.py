import time
from docker import DockerClient
import pytest

from channelfinder import ChannelFinderClient
import logging
from .docker import setup_compose  # noqa: F401
from testcontainers.compose import DockerCompose

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    encoding="utf-8",
)

LOG: logging.Logger = logging.getLogger(__name__)

MAX_WAIT_SECONDS = 180
EXPECTED_DEFAULT_CHANNELS = 32


def check_channel_count(cf_client, name="*", expected_count=EXPECTED_DEFAULT_CHANNELS):
    channels = cf_client.find(name="*")
    LOG.info("Found %s channels", len(channels))
    return len(channels) == expected_count


def wait_for_sync(cf_client, check):
    seconds_to_wait = 1
    total_seconds_waited = 0
    while total_seconds_waited < MAX_WAIT_SECONDS:
        try:
            if check(cf_client):
                break
        except Exception as e:
            LOG.error(e)
        time.sleep(seconds_to_wait)
        total_seconds_waited += seconds_to_wait
        seconds_to_wait += 2


def create_client_and_wait(compose):
    LOG.info("Waiting for channels to sync")
    cf_client = create_client_from_compose(compose)
    wait_for_sync(cf_client, lambda cf_client: check_channel_count(cf_client))
    return cf_client


def create_client_from_compose(compose):
    cf_host, cf_port = compose.get_service_host_and_port("cf")
    cf_url = f"http://{cf_host if cf_host else 'localhost'}:{cf_port}/ChannelFinder"
    # wait for channels to sync
    LOG.info("CF URL: %s", cf_url)
    cf_client = ChannelFinderClient(BaseURL=cf_url)
    return cf_client


@pytest.fixture(scope="class")
def cf_client(setup_compose):  # noqa: F811
    return create_client_and_wait(setup_compose)


class TestE2E:
    def test_smoke(self, cf_client) -> None:
        """
        Test that the setup in the docker compose creates channels in channelfinder
        """
        channels = cf_client.find(name="*")
        assert len(channels) == EXPECTED_DEFAULT_CHANNELS
        assert channels[0]["name"] == "IOC1-1::li"

    # Smoke Test Default Properties
    def test_alias(self, cf_client) -> None:
        """
        Test that the setup in the docker compose creates channels with aliases in channelfinder
        """
        channels = cf_client.find(property=[("alias", "*")])
        assert len(channels) == 8
        assert channels[0]["name"] == "IOC1-1:lix1"
        assert {
            "name": "alias",
            "value": "IOC1-1::li",
            "owner": "admin",
            "channels": [],
        } in channels[0]["properties"]

    def test_desc(self, cf_client) -> None:
        """
        Test that the setup in the docker compose creates channels with descriptions in channelfinder
        """
        channels = cf_client.find(property=[("recordDesc", "*")])
        assert len(channels) == 4
        assert {
            "name": "recordDesc",
            "value": "testdesc",
            "owner": "admin",
            "channels": [],
        } in channels[0]["properties"]

    def test_type(self, cf_client) -> None:
        """
        Test that the setup in the docker compose creates channels with types in channelfinder
        """
        channels = cf_client.find(property=[("recordType", "*")])
        assert len(channels) == EXPECTED_DEFAULT_CHANNELS
        assert {
            "name": "recordType",
            "value": "longin",
            "owner": "admin",
            "channels": [],
        } in channels[0]["properties"]


class TestRemoveProperty:
    def test_bug(self, setup_compose: DockerCompose) -> None:  # noqa: F811
        """
        Test that the setup in the docker compose creates channels in channelfinder
        """
        LOG.info("Waiting for channels to sync")
        cf_client = create_client_and_wait(setup_compose)

        # Check ioc1-1 has ai:archive with info tag "archive"
        LOG.debug('Checking ioc1-1 has ai:archive with info tag "archive"')
        archive_channel_name = "IOC1-1:ai:archive"
        archive_channel = cf_client.find(name=archive_channel_name)

        def get_len_archive_properties(archive_channel):
            return len(
                [
                    prop
                    for prop in archive_channel[0]["properties"]
                    if prop["name"] == "archive"
                ]
            )

        assert get_len_archive_properties(archive_channel) == 1

        # Shutdown ioc1-1docker_client = DockerClient()
        ioc_container = setup_compose.get_container("ioc1-1")
        docker_client = DockerClient()
        new_ioc_image = docker_client.containers.get(ioc_container.ID).commit(
            tag="ioc1-1-bugtest"
        )
        docker_client.containers.get(ioc_container.ID).stop()

        # Restart with st_bugtest.cmd cmd instead
        docker_client.containers.run(
            new_ioc_image,
            command="./demo /recsync/iocBoot/iocdemo/st_bugtest.cmd",
        )

        def check_archive_channel_removed(cf_client):
            archive_channels = cf_client.find(property=[("archive", "MONITOR@1")])
            return len(archive_channels) == 3

        # Check ioc1-1 does not have info tag "archive" on ai:archive
        wait_for_sync(cf_client, check_archive_channel_removed)

        archive_channel = cf_client.find(name=archive_channel_name)
        assert get_len_archive_properties(archive_channel) == 0
