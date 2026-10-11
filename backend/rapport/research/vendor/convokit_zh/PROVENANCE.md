# ConvoKit Chinese politeness extractor

Source: https://github.com/CornellNLP/ConvoKit
Pinned commit: `5dabba5ae034686185d9ff6612e2d57d694bff68`.
License: MIT, preserved in LICENSE. Upstream file hashes are in upstream.json.

Copied the Chinese CSCW feature extractor, shared marker helper, and three lexicons.
Only imports/resource paths and explicit UTF-8 decoding changed for this package.
The adapter supplies jieba tokens instead of the example's spaCy tokenizer; therefore
this is a tokenization adaptation, not a replication of the paper's measured accuracy.
These are observable linguistic markers, not estimates of affection or psychological state.
