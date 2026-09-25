"""D-77: the owner's Live Validator confirmation engine inside MAPEX.

`setup_model`, `evidence` and `plan` are copied from sedijohn135-glitch/live-validator- (app/) with only their
imports changed; `market` and `context` hold the few helpers and the market view they read. `engine` is MAPEX's own:
it runs the validator's decisions on MAPEX data and sends the entry through MAPEX's guarded order path.
"""
