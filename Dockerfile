FROM mambaorg/micromamba:1.4.9 as micromamba

ENV IS_DOCKER true

WORKDIR /app

COPY --chown=$MAMBA_USER:$MAMBA_USER environment.yml environment.yml
RUN micromamba install -y -n base -f environment.yml && \
    micromamba clean --all --yes

COPY --chown=$MAMBA_USER:$MAMBA_USER src src
RUN ["chmod", "+x", "/app/src/entrypoint.sh"]

ENTRYPOINT ["/usr/local/bin/_entrypoint.sh", "/app/src/entrypoint.sh"]