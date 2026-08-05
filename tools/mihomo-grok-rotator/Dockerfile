FROM python:3.12-alpine

RUN addgroup -S -g 10001 quality-guard-reader \
    && adduser -S -D -H -u 10001 -G quality-guard-reader mihomo-rotator \
    && mkdir -p /var/lib/mihomo-grok-rotator \
    && chown -R mihomo-rotator:quality-guard-reader /var/lib/mihomo-grok-rotator \
    && chmod 0700 /var/lib/mihomo-grok-rotator

COPY tools/mihomo-grok-rotator/rotator.py /usr/local/bin/mihomo-grok-rotator
RUN chmod 0755 /usr/local/bin/mihomo-grok-rotator

USER mihomo-rotator
ENTRYPOINT ["/usr/local/bin/mihomo-grok-rotator"]
