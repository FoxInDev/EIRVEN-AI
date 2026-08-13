# EIRVEN Education / RAG data notice

The EIRVEN software license does **not** relicense the texts or third-party datasets stored in, indexed by, or used to build the Education RAG.

## RuHeritage-Corpus

Upstream: `maxzt/RuHeritage-Corpus`

The upstream dataset card identifies:

- the dataset compilation/curation as **CC BY 4.0**;
- the underlying literary works as **Public Domain** according to the provenance statement on that dataset card.

When redistributing an EIRVEN Education RAG built from this source, preserve appropriate attribution to RuHeritage-Corpus and its source/provenance information.

Suggested attribution:

> Contains material indexed from RuHeritage-Corpus by MaxZT, dataset compilation/curation licensed CC BY 4.0; underlying source works identified by the dataset provider as public-domain material. Source provenance originates from Russian Wikisource as described by the upstream dataset card.

## Russian-PD

Upstream: `PleIAs/Russian-PD`

The upstream dataset card identifies the collection as **Public Domain** and states that the texts may be used for model training and republished for reproducibility.

Suggested provenance notice:

> Contains material indexed from PleIAs/Russian-PD, identified by its upstream dataset provider as a public-domain collection.

## Important

Dataset cards and source repositories may change over time. Before publishing a newly rebuilt RAG from a newer upstream revision, re-check the exact upstream provenance and license metadata for the revision actually used.

The generated SQLite/FTS index structure, EIRVEN retrieval code, manifests, and build scripts may contain EIRVEN-authored software/database-structure material, but that does not remove or replace rights and obligations attached to the indexed source texts themselves.
