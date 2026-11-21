# The scorer builds this on linux/amd64 and runs it with no network. The
# digest is the image we labeled every commit in: its Python and SQLite
# decide which sqlglot tests run, so keep it if your image runs them.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90 AS base

FROM base AS checkout
WORKDIR /baseline
RUN python -c 'from urllib.request import urlretrieve; urlretrieve("https://codeload.github.com/tobymao/sqlglot/tar.gz/eb11aec4be8cb2122afe713a4816662c3f0fb2fa", "/tmp/sqlglot.tar.gz")' \
    && tar -xzf /tmp/sqlglot.tar.gz --strip-components=1 \
    && rm /tmp/sqlglot.tar.gz

FROM checkout AS coverage
COPY requirements.lock /tmp/requirements.lock
RUN python -m pip install --no-cache-dir -r /tmp/requirements.lock
COPY build_coverage.py sitecustomize.py /app/
RUN mkdir /coverage \
    && SKIP_INTEGRATION=0 PYTHONPATH=/app BLAST_RADIUS_COVERAGE_DIR=/coverage \
        python -m pytest -q -n 4 --dist loadfile -p build_coverage tests \
    && python /app/build_coverage.py /coverage /coverage.json.gz

FROM base
COPY --from=checkout /baseline/sqlglot /app/baseline/sqlglot
COPY impact.py selector.py coverage.json.gz /app/
CMD ["python", "/app/selector.py"]
