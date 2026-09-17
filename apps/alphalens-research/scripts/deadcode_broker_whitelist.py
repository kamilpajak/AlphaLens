"""Vulture whitelist for ``just deadcode`` (#1471).

Vulture parses this file and never imports it. Each entry marks a name as used
and must carry a reason on its line. Vulture matches by NAME, so an entry also
hides every other symbol with the same name in the scanned code. The report
flags an entry that no longer hides a broker finding.
"""

from vulture.whitelists.whitelist_utils import Whitelist

_ = Whitelist()

# Protocol members. A runtime_checkable Protocol checks that the attribute is
# present, so vulture cannot see the use.
_.place_stop_limit  # SupportsTrailingStop member; control_loop gates entry trailing on isinstance, removing it turns LIVE trailing off silently
_.geometry_name  # ExitPolicy identity property, read by the policy tests; a Protocol member, not checked with isinstance

_.poll_tick  # FillSource member; the seam a streaming fill source will implement, not wired yet on purpose
_.YfinancePriceFeed  # PriceFeed kept on purpose as an interim/fallback price source and test double; not wired yet

# Framework hooks, called by the standard library.
_.do_GET  # BaseHTTPRequestHandler hook of the marketdata-auth callback server
_.log_message  # BaseHTTPRequestHandler hook, silenced so the callback does not print request lines

# The client-manager boundary. The acceptance suite drives the real loop through it.
_.submit_intent  # ManagerService method, called by the acceptance ManagerWorld
_.query_state  # ManagerService method, called by the service tests
_.stream_events  # ManagerService method, drained by the acceptance ManagerWorld
_.run_cycle  # InProcessManagerService method, the acceptance WHEN step
_.owned_qty  # PositionState field returned by query_state
_.heartbeat_ts  # ManagerEvent field emitted each cycle
_.kill_active  # ManagerEvent field emitted each cycle
_.chain_alive  # ManagerEvent field emitted each cycle

# Record fields that are written for a reader outside production code.
_.picks_placed  # TickReport counter, the observable outcome of a tick in tests and the service
_.exits_placed  # TickReport counter, the observable outcome of a tick in tests
_.cancels  # TickReport counter, the observable outcome of a tick in tests
_.verdict_count  # TickReport counter, the observable outcome of a tick in tests
_.audits_deferred  # TickReport counter, the observable outcome of a tick in tests
_.decision_mid  # ExecQualityRecord column, written to parquet through asdict
_.retires_when  # LegacyAllowance field, read through a string loop in legacy.py
_.fractional_enabled  # InstrumentQuantityRules reports what the venue said verbatim; policy decides later, like round_lot

# Test hooks and documented bounds.
_._reset_remote_quote_source_for_tests  # test hook that resets the module-level quote source
_.COMPACTED_LINES_PER_CRID_BOUND  # documented compaction bound that the entry-trail tests assert
_.open_watch  # tested EntryTierWatcher constructor; production opens watches in control_loop._open_entry_watches
_.get_user  # SaxoClient auth read used by the hermetic client tests and the SIM live probe
