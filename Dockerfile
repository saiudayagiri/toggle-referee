# Referee has no dependencies beyond Python itself, so the image is just the
# source on a slim Python base.
FROM python:3.12-slim
WORKDIR /opt/referee
COPY pyproject.toml README.md ./
COPY referee ./referee
RUN pip install --no-cache-dir .
WORKDIR /work
ENTRYPOINT ["referee"]
CMD ["--help"]
