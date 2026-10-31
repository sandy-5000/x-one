document.addEventListener("click", async (event) => {
  const button = event.target.closest(".copy-btn");
  if (!button) return;

  const text = button.parentElement.querySelector("pre").innerText;
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    // navigator.clipboard is unavailable over plain http on non-localhost hosts.
    const area = document.createElement("textarea");
    area.value = text;
    area.style.position = "fixed";
    area.style.opacity = "0";
    document.body.appendChild(area);
    area.select();
    document.execCommand("copy");
    area.remove();
  }

  button.textContent = "Copied";
  button.classList.add("is-copied");
  clearTimeout(button.resetTimer);
  button.resetTimer = setTimeout(() => {
    button.textContent = "Copy";
    button.classList.remove("is-copied");
  }, 1500);
});
