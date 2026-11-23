/* Puts the search box where it belongs for the screen: at the right-hand end of the tab bar's row on a desktop (1100px and wider),
   in the header on smaller screens. A file, not inline, for the Content-Security-Policy; it runs as soon as the tab bar has been
   read, before the page first paints, so the box never visibly jumps. */
(function () {
  var box = document.getElementById("gsearch"), row = document.getElementById("navrow"),
    home = document.querySelector(".topbar-inner"), after = document.querySelector(".status-row");
  if (!box || !row || !home || !after || !window.matchMedia) return;
  var mq = window.matchMedia("(min-width: 1100px)");
  function place() {
    var held = box.contains(document.activeElement) ? document.activeElement : null;        // a window resized while you are typing
    if (mq.matches) { if (box.parentNode !== row) row.appendChild(box); }
    else if (box.parentNode !== home) home.insertBefore(box, after);
    if (held) held.focus({ preventScroll: true });
  }
  place();
  if (mq.addEventListener) mq.addEventListener("change", place); else mq.addListener(place);
})();
