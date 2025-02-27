import logging
import pytest
from testcontainers.compose import DockerCompose

from docker import DockerClient
import pathlib

LOG: logging.Logger = logging.getLogger(__name__)


def fullSetupDockerCompose() -> DockerCompose:
    current_path = pathlib.Path(__file__).parent.resolve()

    return DockerCompose(
        str(current_path.parent.resolve()),
        compose_file_name=str(
            current_path.parent.joinpath("docker-compose.yml").resolve()
        ),
        build=True,
    )


def log_container_logs(compose: DockerCompose) -> None:
    docker_client = DockerClient()
    conts = {container.ID: container for container in compose.get_containers()}
    for cont_id, cont in conts.items():
        log = docker_client.containers.get(cont_id).logs()
        LOG.debug("Info for container %s", cont)
        LOG.debug("Logs for container %s", cont.Name)
        LOG.debug(log.decode("utf-8"))


@pytest.fixture(scope="class")
def setup_compose():
    LOG.info("Setup test environment")
    compose = fullSetupDockerCompose()
    compose.start()
    yield compose
    LOG.info("Teardown test environment")
    LOG.info("Stopping docker compose")
    if LOG.level <= logging.DEBUG:
        log_container_logs(compose)
    compose.stop()
