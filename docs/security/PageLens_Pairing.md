# PageLens local pairing

The desktop bridge and browser extension must be deployed together. Without a token of at least 32 characters and a nonempty allowed-Origin list, the desktop bridge does not start a listener.

## Configuration

- Set `FIREFLY_PAGELENS_TOKEN` to a locally generated URL-safe random credential of at least 32 characters. Do not reuse a model API credential or publish it in a repository, URL, screenshot or log.
- Set `FIREFLY_PAGELENS_ORIGINS` to exact browser-extension Origins, such as `chrome-extension://<extension-id>`. Separate multiple Origins with commas; wildcards are not supported.
- Set the same credential as `fireflyBridgeToken` in the extension's `chrome.storage.local`, then reload the extension and restart Firefly. Webpage messages cannot set this value.
- The extension sends `firefly-auth.<credential>` as a WebSocket subprotocol. The desktop compares the credential in constant time and requires the exact Origin. Rotation requires both sides to change and old connections to close.

## Limits and scope

The paired service listens only on `127.0.0.1:17321`, permits one client and two simultaneous AI requests, limits messages to 64 KiB and 32 chat entries, limits the receive queue to four, and disables compression. Unauthorized handshakes return HTTP 403. Additional clients are refused; oversized WebSocket messages close with code 1009. Provider errors return a generic status without provider exception bodies. Results from disconnected requests cannot be delivered to a replacement connection.

Offline wire tests use a test-owned loopback listener and a local handler, not an external model. This configuration does not protect against a malicious program already able to read the same user's environment or extension storage. Origin alone is not authentication.
