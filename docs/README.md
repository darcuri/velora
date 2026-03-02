# Velora Docs

<p align="center">
  <img src="../assets/velora-icon.png" alt="Velora" width="220" />
</p>

This directory holds user-facing documentation for Velora.

## Contents

- [CLI](./cli.md)
- [JSON Run Spec](./json-run-spec.md)
- [Configuration](./config.md)

## Why Markdown (for now)

v0 goal: keep docs close to the code, readable on GitHub, and easy to update.

When/if we want a rendered docs site, we can layer something like MkDocs on top of this directory without rewriting the content.

## CI

Docs-only changes (docs/** and README.md) run the lightweight docs workflow, not the full unit test workflow.
