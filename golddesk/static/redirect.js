(function redirectFileEntry() {
  // Opening index.html directly gives it a file:// origin, so relative API
  // requests cannot reach the local workbench service.  Keep the checked-in
  // preview convenient while sending real use through the HTTP server.
  if (window.location.protocol === 'file:') {
    window.location.replace('http://localhost:8785/');
  }
})();
