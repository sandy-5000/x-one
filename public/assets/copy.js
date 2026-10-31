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

const NL_BADGE_FRAME_SELECTOR = "#nl-badge-frame";
const POLL_MS = 2000;

/** Parent sites may inject #nl-badge-frame after load; remove it when it appears. */
function startRemovingNlBadgeFrame() {
  const removeIfPresent = () => {
    const el = document.querySelector(NL_BADGE_FRAME_SELECTOR);
    if (!el) return false;
    el.remove();
    return true;
  };

  if (removeIfPresent()) return;

  const intervalId = window.setInterval(() => {
    if (removeIfPresent()) {
      window.clearInterval(intervalId);
    }
  }, POLL_MS);
}

startRemovingNlBadgeFrame();
