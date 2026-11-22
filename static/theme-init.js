/* Applies the saved light theme before the page paints, so there is no flash. Kept as a file (not inline) for the CSP. */
try{if(localStorage.getItem("evileye-theme")==="light")document.documentElement.dataset.theme="light"}catch(e){}
