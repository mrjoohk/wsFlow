# Google authorization refresh input contract

During the OAuth polling callback, status lookup, Google refresh and the following calendar refresh form one active UI operation. Reuse the existing busy state so a selection action cannot be submitted while this refresh is in progress. Restore admission on both success and failure. The existing busy/visibility checks, API calls and timer cadence remain unchanged.

The guard covers this authorization callback only. It does not promise to preserve every unsaved checkbox across every background redraw or account change. Browser teardown deadlines and application unit deadlines remain unchanged.

Verify held status, refresh and calendar promises by attempting the actual selection action: no select request is sent until release, and a later action works. Verify failure cleanup and an already-active user action. A controlled real-browser range response must show disabled input during the refresh and preserve the subsequently saved calendar choice. Browser shutdown success is a separate result.

Before a later action can redraw the UI, verify that the last rendered controls are enabled after success and each failure. Use the registered selection handler to check the submitted calendar IDs. For pending statuses true, true, then false, verify that each tick releases busy and the next tick sends another status request. Removing the final redraw, suppressing it on error, or releasing busy only when pending is false must fail the contract test.
