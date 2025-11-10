import logging
import threading
from pathlib import Path

from channelfinder import ChannelFinderClient
from testcontainers.compose import DockerCompose

from docker import DockerClient
from docker.models.containers import Container

from .client_checks import (
    BASE_IOC_CHANNEL_COUNT,
    INACTIVE_PROPERTY,
    check_channel_property,
    create_client_and_wait,
    wait_for_sync,
)
from .docker import ComposeFixtureFactory

LOG: logging.Logger = logging.getLogger(__name__)

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    encoding="utf-8",
)

BASE_CHANNEL_COUNT = 9
setup_compose = ComposeFixtureFactory(Path("docker") / Path("test-bash-ioc.yml")).return_fixture()


def docker_exec_new_command(container: Container, command: str):
    def stream_logs(exec_result, cmd: str):
        if LOG.level <= logging.DEBUG:
            LOG.debug("Logs from %s with command %s", container.name, cmd)
            for line in exec_result.output:
                LOG.debug(line.decode().strip())

    exec_result = container.exec_run(command, tty=True, stream=True)
    log_thread = threading.Thread(
        target=stream_logs,
        args=(
            exec_result,
            command,
        ),
    )
    log_thread.start()


def start_ioc(setup_compose: DockerCompose) -> Container:
    ioc_container = setup_compose.get_container("ioc1-1")
    docker_client = DockerClient()
    docker_ioc = docker_client.containers.get(ioc_container.ID)
    docker_exec_new_command(docker_ioc, "./demo /recsync/iocBoot/iocdemo/st_base.cmd")
    return docker_ioc


def restart_ioc(
    ioc_container: Container, cf_client: ChannelFinderClient, base_channel_name: str, new_st: str
) -> Container:
    ioc_container.stop()
    LOG.info("Waiting for channels to go inactive")
    assert wait_for_sync(
        cf_client,
        lambda cf_client: check_channel_property(cf_client, name=base_channel_name, prop=INACTIVE_PROPERTY),
    )
    ioc_container.start()

    docker_exec_new_command(ioc_container, f"./demo /recsync/iocBoot/iocdemo/{new_st}.cmd")
    # Detach by not waiting for the thread to finish

    LOG.debug("ioc1-1 restart")
    assert wait_for_sync(cf_client, lambda cf_client: check_channel_property(cf_client, name=base_channel_name))
    LOG.debug("ioc1-1 has restarted and synced")


class TestRemoveInfoTag:
    def test_remove_infotag(self, setup_compose: DockerCompose) -> None:  # noqa: F811
        """
        Test that removing an infotag from a record works
        """
        # Arrange
        docker_ioc = start_ioc(setup_compose)
        LOG.info("Waiting for channels to sync")
        cf_client = create_client_and_wait(setup_compose, expected_channel_count=BASE_IOC_CHANNEL_COUNT)

        # Check before
        LOG.debug('Checking ioc1-1 has ai:base_record with info tag "archive"')
        base_channel_name = "IOC1-1:ai:base_record"
        base_channel = cf_client.find(name=base_channel_name)

        def get_len_archive_properties(base_channel):
            return len([prop for prop in base_channel[0]["properties"] if prop["name"] == "archive"])

        assert get_len_archive_properties(base_channel) == 1

        # Act
        restart_ioc(docker_ioc, cf_client, base_channel_name, "st_remove_infotag")

        # Assert
        base_channel = cf_client.find(name=base_channel_name)
        LOG.debug("archive channel: %s", base_channel)
        assert get_len_archive_properties(base_channel) == 0


class TestRemoveChannel:
    def test_remove_channel(self, setup_compose: DockerCompose) -> None:  # noqa: F811
        """
        Test that removing a channel works correctly.
        """
        # Arrange
        docker_ioc = start_ioc(setup_compose)
        LOG.info("Waiting for channels to sync")
        cf_client = create_client_and_wait(setup_compose, expected_channel_count=BASE_IOC_CHANNEL_COUNT)

        # Check ioc1-1 has base channel
        LOG.debug('Checking ioc1-1 has ai:base_record2"')
        base_channel_name = "IOC1-1:ai:base_record"
        base_channel_test_name = "IOC1-1:ai:base_record2"
        check_channel_property(cf_client, name=base_channel_name)
        check_channel_property(cf_client, name=base_channel_test_name)

        # Act
        restart_ioc(docker_ioc, cf_client, base_channel_name, "st_remove_channel")

        # Assert
        check_channel_property(cf_client, name=base_channel_name)
        check_channel_property(cf_client, name=base_channel_test_name, prop=INACTIVE_PROPERTY)
