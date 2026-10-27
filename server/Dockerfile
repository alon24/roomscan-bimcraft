FROM rust:1 AS cli
ARG BIMCRAFT_COMMIT=b36160b
RUN git clone https://github.com/IPedrax/BIMCraft /src && cd /src && git checkout $BIMCRAFT_COMMIT \
    && cargo build --release -p bimcraft-cli

FROM python:3.12-slim
WORKDIR /app
COPY server/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY --from=cli /src/target/release/bimcraft-cli /usr/local/bin/bimcraft-cli
COPY server/app ./app
ARG APP_VERSION=dev
ARG APP_BUILD=0
ENV APP_VERSION=$APP_VERSION APP_BUILD=$APP_BUILD
ENV BIMCRAFT_CLI=/usr/local/bin/bimcraft-cli
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
