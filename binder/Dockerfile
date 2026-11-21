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

ARG JUPYTERHUB_VERSION=6.2.0
RUN pip install --no-cache-dir "jupyterhub==${JUPYTERHUB_VERSION}"

ARG NB_USER=jovyan
ARG NB_UID=1000
ENV USER=${NB_USER} \
    HOME=/home/${NB_USER}
RUN useradd --create-home --uid "${NB_UID}" "${NB_USER}" \
    && cp -r /examples "/home/${NB_USER}/examples" \
    && chown -R "${NB_UID}:${NB_UID}" "/home/${NB_USER}"
WORKDIR /home/${NB_USER}
USER ${NB_USER}
