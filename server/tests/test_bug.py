import pathlib
import time
from docker import DockerClient
from testcontainers.compose import DockerCompose

from channelfinder import ChannelFinderClient
import logging

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    encoding="utf-8",
)

LOG: logging.Logger = logging.getLogger(__name__)

MAX_WAIT_SECONDS = 180
EXPECTED_DEFAULT_CHANNELS = 24


def fullSetupDockerCompose() -> DockerCompose:
    current_path = pathlib.Path(__file__).parent.resolve()

    return DockerCompose(
        str(current_path.parent.resolve()),
        compose_file_name=str(
            current_path.parent.joinpath("docker-compose.yml").resolve()
        ),
    )


class TestE2E:
    compose: DockerCompose

    def setup_method(self) -> None:
        """Setup the test environment"""
        LOG.info("Setting up test")
        self.compose = fullSetupDockerCompose()
        self.compose.start()

    def teardown_method(self) -> None:
        """Teardown the test environment"""

        LOG.info("Tearing down test")
        if self.compose:
            LOG.info("Stopping docker compose")
            if LOG.level <= logging.DEBUG:
                docker_client = DockerClient()
                conts = {
                    container.ID: container
                    for container in self.compose.get_containers()
                }
                for cont_id, cont in conts.items():
                    log = docker_client.containers.get(cont_id).logs()
                    LOG.debug("Info for container %s", cont)
                    LOG.debug("Logs for container %s", cont.Name)
                    LOG.debug(log.decode("utf-8"))
            self.compose.stop()

    def test_bug(self) -> None:
        """
        Test that the setup in the docker compose creates channels in channelfinder
        """
        LOG.info("Waiting for channels to sync")
        cf_host, cf_port = self.compose.get_service_host_and_port("cf")
        cf_url = f"http://{cf_host if cf_host else 'localhost'}:{cf_port}/ChannelFinder"
        # wait for channels to sync
        LOG.info("CF URL: %s", cf_url)
        cf_client = ChannelFinderClient(BaseURL=cf_url)
        self.wait_for_sync(cf_client)
        channels = cf_client.find(name="*")
        assert len(channels) == EXPECTED_DEFAULT_CHANNELS
        assert channels[0]["name"] == "IOC1-1::li"

        # Check ioc1-1 has ai:archive with info tag "archive"
        LOG.debug('Checking ioc1-1 has ai:archive with info tag "archive"')
        archive_channel_name = "IOC1-1:ai:archive"
        archive_channel = cf_client.find(name=archive_channel_name)
        assert self.get_len_archive_properties(archive_channel) == 1

        # Shutdown ioc1-1docker_client = DockerClient()
        ioc_container = self.compose.get_container("ioc1-1")
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

        # Check ioc1-1 does not have info tag "archive" on ai:archive
        self.wait_for_sync(
            cf_client, lambda cf: cf.find(property=[("archive", "MONITOR@1")]), 3
        )
        archive_channel = cf_client.find(name=archive_channel_name)
        assert self.get_len_archive_properties(archive_channel) == 0

    def get_len_archive_properties(self, archive_channel):
        return len(
            [
                prop
                for prop in archive_channel[0]["properties"]
                if prop["name"] == "archive"
            ]
        )

    def wait_for_sync(
        self,
        cf_client,
        method=lambda cf: cf.find(name="*"),
        expected_count=EXPECTED_DEFAULT_CHANNELS,
    ) -> None:
        seconds_to_wait = 1
        total_seconds_waited = 0
        while total_seconds_waited < MAX_WAIT_SECONDS:
            try:
                channels = method(cf_client)
                LOG.info(
                    "Found %s channels for %s in %s seconds, expected %s",
                    len(channels),
                    method,
                    total_seconds_waited,
                    expected_count,
                )
                if len(channels) == expected_count:
                    break
            except Exception as e:
                LOG.error(e)
            time.sleep(seconds_to_wait)
            total_seconds_waited += seconds_to_wait
            seconds_to_wait += 2
