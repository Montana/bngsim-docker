# What mybinder.org builds for the "launch in the browser" link: the bngsim
# jupyter image (bngsim, BioNetGen, JupyterLab, the examples), set up as Binder
# needs it. Nothing to install: a student clicks the link and gets JupyterLab.
#
#   https://mybinder.org/v2/gh/Montana/bngsim-docker/binder?urlpath=lab/tree/examples/tour.ipynb
#
# Binder runs the server itself, as an ordinary user with uid 1000 and through
# jupyterhub-singleuser; the examples are copied into that user's home, so the
# file browser opens on them and a student's edits can be saved.
FROM montanamendy/bngsim:jupyter

# The image runs as its own user (bngsim); installing and adding Binder's user
# need root, and the last line hands the server to Binder's user.
# hadolint ignore=DL3002
USER root
ARG JUPYTERHUB_VERSION=6.2.0
RUN pip install --no-cache-dir "jupyterhub==${JUPYTERHUB_VERSION}"

ARG NB_USER=jovyan
ARG NB_UID=1000
ENV USER=${NB_USER} \
    HOME=/home/${NB_USER}
# The image's own user, bngsim, holds uid 1000, which Binder's user needs.
RUN userdel bngsim \
    && useradd --create-home --uid "${NB_UID}" "${NB_USER}" \
    && cp -r /examples "/home/${NB_USER}/examples" \
    && chown -R "${NB_UID}:${NB_UID}" "/home/${NB_USER}"
WORKDIR /home/${NB_USER}
USER ${NB_USER}
