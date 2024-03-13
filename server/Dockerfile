FROM python:3.8 as build

WORKDIR /home/recceiver

COPY . ./server

RUN python -m venv venv

RUN venv/bin/python -m pip install ./server

FROM python:3.8-slim

WORKDIR /home/recceiver

COPY --from=build /home/recceiver/venv venv

CMD venv/bin/twistd --pidfile= --nodaemon recceiver --config cf.conf

