window.FLIXORA_CONFIG = {
  // Frontend is deployed separately as a static site, so API calls must target
  // the live backend service explicitly instead of the current page origin.
  apiBase: "https://feemx.onrender.com",
  preferConfiguredApiInLocalDev: true,
};
