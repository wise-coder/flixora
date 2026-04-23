window.FLIXORA_CONFIG = {
  // Force the frontend to call the same public origin that served the page.
  // This is the safest setup when frontend and backend are deployed as one service.
  apiBase: window.location.origin,
  preferConfiguredApiInLocalDev: true,
};
