// SPDX-License-Identifier: AGPL-3.0-only

(() => {
  "use strict";

  const root = document.documentElement;
  const themeToggle = document.querySelector("[data-theme-toggle]");
  const liveRegion = document.querySelector("[data-live-region]");
  const dialImage = document.querySelector(".hero-dial");
  const dialSource = document.querySelector("[data-dial-source]");
  const darkQuery = window.matchMedia("(prefers-color-scheme: dark)");
  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");

  const announce = (message) => {
    if (!liveRegion) return;
    liveRegion.textContent = "";
    window.setTimeout(() => {
      liveRegion.textContent = message;
    }, 30);
  };

  const currentTheme = () => {
    const explicit = root.dataset.theme;
    if (explicit === "dark" || explicit === "light") return explicit;
    return darkQuery.matches ? "dark" : "light";
  };

  const updateThemeLabel = () => {
    if (!themeToggle) return;
    const next = currentTheme() === "dark" ? "light" : "dark";
    const label = `Switch to ${next} theme`;
    themeToggle.setAttribute("aria-label", label);
    themeToggle.setAttribute("title", label);
  };

  const updateDialTheme = () => {
    if (!dialImage || !dialSource) return;
    const explicit = root.dataset.theme;
    if (explicit === "light" || explicit === "dark") {
      dialSource.media = "not all";
      dialImage.src = explicit === "light"
        ? "assets/iris-dial-light.gif"
        : "assets/iris-dial.gif";
      return;
    }
    dialSource.media = "(prefers-color-scheme: light)";
    dialImage.src = "assets/iris-dial.gif";
  };

  try {
    const savedTheme = window.localStorage.getItem("iris-theme");
    if (savedTheme === "dark" || savedTheme === "light") {
      root.dataset.theme = savedTheme;
    }
  } catch (_error) {
    // Theme persistence is optional; the system preference remains authoritative.
  }
  updateThemeLabel();
  updateDialTheme();

  themeToggle?.addEventListener("click", () => {
    const next = currentTheme() === "dark" ? "light" : "dark";
    root.dataset.theme = next;
    try {
      window.localStorage.setItem("iris-theme", next);
    } catch (_error) {
      // A privacy-restricted browser may reject localStorage.
    }
    updateThemeLabel();
    updateDialTheme();
    announce(`${next[0].toUpperCase()}${next.slice(1)} theme enabled.`);
  });

  darkQuery.addEventListener?.("change", () => {
    updateThemeLabel();
    updateDialTheme();
  });

  const fallbackCopy = (text) => {
    const field = document.createElement("textarea");
    field.value = text;
    field.setAttribute("readonly", "");
    field.style.position = "fixed";
    field.style.opacity = "0";
    document.body.append(field);
    field.select();
    const copied = document.execCommand("copy");
    field.remove();
    return copied;
  };

  document.querySelectorAll("[data-copy-target]").forEach((button) => {
    button.addEventListener("click", async () => {
      const target = document.getElementById(button.dataset.copyTarget);
      if (!target) return;
      const text = target.innerText.trim();
      let copied = false;
      try {
        await navigator.clipboard.writeText(text);
        copied = true;
      } catch (_error) {
        copied = fallbackCopy(text);
      }
      const original = button.textContent;
      button.textContent = copied ? "Copied" : "Select text";
      announce(copied ? "Installation commands copied." : "Copying was blocked by your browser.");
      window.setTimeout(() => {
        button.textContent = original;
      }, 1800);
    });
  });

  const header = document.querySelector(".site-header");
  const updateHeader = () => header?.classList.toggle("is-scrolled", window.scrollY > 12);
  updateHeader();
  window.addEventListener("scroll", updateHeader, { passive: true });

  if (!reducedMotion.matches && "IntersectionObserver" in window) {
    const candidates = [...document.querySelectorAll("[data-reveal]")];
    const observer = new IntersectionObserver((entries) => {
      entries.forEach((entry) => {
        if (!entry.isIntersecting) return;
        entry.target.classList.add("reveal-visible");
        entry.target.classList.remove("reveal-pending");
        observer.unobserve(entry.target);
      });
    }, { rootMargin: "0px 0px -8%", threshold: 0.08 });

    candidates.forEach((element) => {
      if (element.getBoundingClientRect().top > window.innerHeight * 0.86) {
        element.classList.add("reveal-pending");
        observer.observe(element);
      }
    });
  }
})();
