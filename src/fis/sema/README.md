# STAGING SNAPSHOT — PLEASE ONLY USE IN DEV

This snapshot contains STAGING vocabulary: mutable words that run on dev
brokers only. It MUST NOT be used against hybrid or production brokers.

Staging words in this snapshot:

- type fis.connect.claims:000
- type g.node.instance.gt:001

When these words promote to published, rebuild without `--allow-staged` to
get a publication-grade snapshot (and this file disappears).
