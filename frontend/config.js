window.LUCA_CONFIG = window.LUCA_CONFIG || {
  apiBaseUrl: window.location.origin.includes("localhost")
    ? "http://127.0.0.1:8000"
    : "https://REPLACE_WITH_API_GATEWAY_URL"
};
