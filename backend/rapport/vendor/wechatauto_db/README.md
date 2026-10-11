# wechatauto-replica database engine

Vendored `wechatauto/db.py` from PyPI **1.2.4.3**, Apache-2.0 (see LICENSE).
Source: https://github.com/fanyuantaier/wechatauto-replica
Wheel SHA-256: a0152feb8de8345a697a5d189e8efb6bc1c67bc3b1f6adfc2b2d9f3d19d36e9e

This is the database dependency used by dsh-wechat-bot. The only vendor-file
change is replacing the upstream logger import with a standard Python logger,
so importing the database engine never imports the upstream GUI package.

Use `rapport.desktop.dsh_database.open_database`, not the base constructor.
That adapter pins one account, encrypts its key cache, restricts database files,
rejects stale/partial snapshots, preserves shard identity, and does not load the
GUI driver's process-memory write code. The upstream Listener is not started:
Rapport commits encrypted message batches and composite cursors together.

Plaintext SQLite working snapshots exist only in the private connection work
directory while connected. Disconnect and application shutdown remove it.
The imported application messages and persisted sync state remain encrypted.
