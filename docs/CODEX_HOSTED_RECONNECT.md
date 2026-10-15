# Connect a Codex account from hosted Katcha

Codex sign-in returns the browser to its registered address,
`http://localhost:1457/auth/callback`. A hosted Katcha server cannot receive
that browser-local callback automatically. Signing in alone does not update the
hosted server's saved connection.

Katcha Settings starts sign-in in another tab and retains the initiating state
in the original tab. After sign-in, copy the complete return address from the
address bar, even if the browser says localhost cannot be reached. Paste it into
the original Katcha Settings tab and choose the completion action. The address
contains a short-lived, single-use sign-in code: enter it only in Katcha Settings,
never in chat, logs, or support messages. Finish within ten minutes; restart
Reconnect if the sign-in expires. An existing saved connection stays in place
until the replacement sign-in completes.

If a local Katcha instance also owns port 1457, it must preserve callbacks for
sign-ins started by the hosted instance. Updated local versions show a completion
page and keep the original address in the address bar without exchanging the
code. Older versions redirect to `settings?chatgpt=error` and lose that address.
Update the local API before starting a fresh hosted sign-in in that case; the
final settings error address cannot complete sign-in.

The completion API requires the existing operator authentication. It accepts
only the registered loopback address and the state retained by the initiating
tab. It never fetches the supplied address. The server exchanges the code with
its stored PKCE verifier and enforces expiry and single use before saving
credentials encrypted in the existing connection store. No new provider redirect
registration, credential file import, or paid provider route is involved.

A successful reconnect establishes saved credentials. A separate connection test
checks actual provider access; quota and inference availability can still vary.
Automated tests use fixture credentials and do not prove a live sign-in.
