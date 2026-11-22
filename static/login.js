/* Sign-in page: the error message for ?error=N, and the theme button. Kept as a file (not inline) for the CSP. */
const MSG = {
  "1": "Incorrect username or password.",
  "2": "Too many failed attempts. Try again in 15 minutes.",
  "3": "The user database is unavailable. Try again shortly."
};
const code = new URLSearchParams(location.search).get("error");
if (code) {
  const el = document.getElementById("login-error");
  el.textContent = MSG[code] || MSG["1"];
  el.hidden = false;
  document.getElementById("password").focus();
}
document.getElementById("theme").addEventListener("click", () => {
  const light = document.documentElement.dataset.theme !== "light";
  if (light) document.documentElement.dataset.theme = "light"; else delete document.documentElement.dataset.theme;
  try { localStorage.setItem("evileye-theme", light ? "light" : "dark"); } catch (e) { /* ignore */ }
});
