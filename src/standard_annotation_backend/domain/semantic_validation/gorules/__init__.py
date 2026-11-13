"""Check annotations against the write-blocking GO Rules.

GO Rules are GO's published checks on annotation quality, each documented on a
go-site page. The write-blocking rules are the ones SABE can check using only one
annotation and the reference data, so SABE rejects any write that breaks one.

Each rule is a function named after its GO Rule, such as `gorule_0000002`, that
takes an annotation and its facts and yields the issues it finds. `rules.RULES`
registers each one with its issue type, title, and details shape. The rules are
grouped by what they examine: the annotated term and evidence (`terms`), the
`with_or_from` values and references (`support`), and the go-site metadata
(`metadata`).
"""
