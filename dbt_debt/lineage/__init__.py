"""Column-lineage sources behind one interface.

`sqlglot` reconstruction is the baseline (`sqlglot_source`); a Fusion artifact reader is the
planned optional, faster source. Both produce `ColumnEdge`s, so the verdict layer never knows
which one ran.
"""
