import { api } from "./api.js";
import { isAuthorizationUrl } from "./tab-account.js";

const $ = selector => document.querySelector(selector);
let authStatus = null;

function showError(error, secret = "") {
  let message = String(error?.message || "No se pudo completar la solicitud.");
  if (secret) message = message.replaceAll(secret, "[oculto]");
  message = message.replace(/(client[_-]?secret|access[_-]?token|refresh[_-]?token)([\s=:]+)[^\s&"'<>]+/gi,
    "$1$2[oculto]");
  $("#login-error").textContent = message;
  $("#login-error").hidden = false;
}

function clearError() {
  $("#login-error").textContent = "";
  $("#login-error").hidden = true;
}

function setBusy(formOrButton, busy, label) {
  const controls = formOrButton instanceof HTMLFormElement
    ? [...formOrButton.querySelectorAll("button, input")]
    : [formOrButton];
  for (const control of controls) control.disabled = busy;
  if (!(formOrButton instanceof HTMLFormElement)) {
    formOrButton.dataset.idleLabel ||= formOrButton.textContent;
    formOrButton.textContent = busy ? label : formOrButton.dataset.idleLabel;
  }
}

async function startGitHub(secret = "") {
  const button = $("#login-authorize");
  setBusy(button, true, "Abriendo GitHub…");
  clearError();
  try {
    const result = await api("auth/github/start", { method: "POST", body: {} });
    if (!isAuthorizationUrl("github", result?.authorization_url)) {
      throw new Error("Relay devolvió una dirección de autorización no válida.");
    }
    window.location.assign(result.authorization_url);
    return true;
  } catch (error) {
    showError(error, secret);
    setBusy(button, false);
    return false;
  }
}

async function saveCredentials(form) {
  const clientId = form.elements.client_id.value.trim();
  const secretInput = form.elements.client_secret;
  let secret = secretInput.value;
  setBusy(form, true);
  clearError();
  try {
    const result = await api("auth/setup", {
      method: "POST",
      body: { client_id: clientId, client_secret: secret },
    });
    if (result?.configured !== true) {
      throw new Error("Relay no confirmó que guardó la configuración.");
    }
    secretInput.value = "";
    authStatus = { ...authStatus, configured: true };
    $("#login-app-instructions").hidden = true;
    $("#login-initial-credentials").hidden = true;
    $("#login-configured").hidden = false;
    $("#login-edit-details").hidden = !authStatus.setup_required || !authStatus.local_setup;
    $("#login-configured-title").textContent = authStatus.setup_required
      ? "GitHub está configurado" : "Inicia sesión con GitHub";
    $("#login-configured-copy").textContent = authStatus.setup_required
      ? "Si la credencial guardada no funciona, puedes reemplazarla abajo y volver a intentarlo."
      : "Confirma tu identidad con GitHub para entrar a esta instalación.";
    $("#login-authorize").textContent = authStatus.setup_required
      ? "Continuar con GitHub" : "Iniciar sesión con GitHub";
    $("#login-authorize").dataset.idleLabel = $("#login-authorize").textContent;
    $("#login-status").textContent = "Configuración guardada en esta instalación.";
    if (!await startGitHub(secret)) setBusy(form, false);
  } catch (error) {
    showError(error, secret);
    secretInput.value = "";
    setBusy(form, false);
  } finally {
    secret = "";
  }
}

async function loadStatus() {
  clearError();
  $("#login-retry").hidden = true;
  $("#login-status").textContent = "Comprobando acceso…";
  try {
    authStatus = await api("auth/status");
    if (authStatus.authenticated) {
      window.location.replace("/admin/");
      return;
    }
    if (!authStatus.local_setup && (authStatus.setup_required || !authStatus.configured)) {
      $("#login-setup").hidden = true;
      $("#login-local-only").hidden = false;
      $("#login-status").textContent = "La configuración inicial requiere acceso local.";
      return;
    }

    $("#login-setup").hidden = false;
    $("#login-app-instructions").hidden = authStatus.configured;
    $("#login-setup-header").hidden = authStatus.configured && !authStatus.setup_required;
    $("#login-homepage-url").textContent = new URL("/admin/", window.location.href).href;
    const callback = $("#login-callback-url");
    if (callback) callback.textContent = authStatus.callback_url || "No se pudo obtener la URL de callback.";
    if (!authStatus.configured) {
      $("#login-intro").textContent = "Vincula esta instalación con GitHub para crear la primera sesión administradora.";
      $("#login-setup-copy").textContent = "La primera persona que autorice este acceso quedará como administradora de esta instalación.";
      $("#login-initial-credentials").hidden = false;
      $("#login-configured").hidden = true;
      $("#login-status").textContent = authStatus.enabled
        ? "Falta completar la configuración de GitHub."
        : "Configura GitHub para iniciar el acceso.";
      return;
    }

    $("#login-intro").textContent = authStatus.setup_required
      ? "GitHub está configurado; falta completar el primer acceso de administración."
      : "Autoriza tu cuenta de GitHub para abrir tu sesión de Relay.";
    $("#login-setup-copy").textContent = authStatus.setup_required
      ? "Si la credencial guardada no funciona, puedes reemplazarla abajo y volver a intentarlo."
      : "Se abrirá GitHub para confirmar tu identidad y volver a Relay.";
    $("#login-initial-credentials").hidden = true;
    $("#login-configured").hidden = false;
    $("#login-edit-details").hidden = !authStatus.setup_required || !authStatus.local_setup;
    $("#login-configured-title").textContent = authStatus.setup_required
      ? "GitHub está configurado" : "Inicia sesión con GitHub";
    $("#login-configured-copy").textContent = authStatus.setup_required
      ? "Si la credencial guardada no funciona, puedes reemplazarla abajo y volver a intentarlo."
      : "Confirma tu identidad con GitHub para entrar a esta instalación.";
    $("#login-authorize").textContent = authStatus.setup_required
      ? "Continuar con GitHub" : "Iniciar sesión con GitHub";
    $("#login-authorize").dataset.idleLabel = $("#login-authorize").textContent;
    $("#login-status").textContent = "";
  } catch (error) {
    $("#login-status").textContent = "No se pudo consultar el acceso de Relay.";
    showError(error);
    $("#login-retry").hidden = false;
  }
}

$("#login-setup-form").addEventListener("submit", event => {
  event.preventDefault();
  saveCredentials(event.currentTarget);
});
$("#login-edit-form").addEventListener("submit", event => {
  event.preventDefault();
  saveCredentials(event.currentTarget);
});
$("#login-authorize").addEventListener("click", () => startGitHub());
$("#login-retry").addEventListener("click", loadStatus);
loadStatus();
