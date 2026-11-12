"""Check annotations against the write-blocking GO Rules.

Each rule is a function named after its GO Rule, such as `gorule_0000002`, that
takes an annotation and its facts and yields the issues it finds. The rules are
grouped by what they examine: the annotated term and evidence (`terms`), the
`with_or_from` values and references (`support`), and the go-site metadata
(`metadata`).
"""
