"use strict";

/*
 * SabTokenManagement is defined separately and exported when possible so that it can be
 * tested in isolation.
 *
 * See:
 *     * tests/javascript/token_management.test.cjs
 */
const SabTokenManagement = (() => {
  function addUtcCalendarMonths(date, months) {
    const result = new Date(date);
    const day = result.getUTCDate();
    result.setUTCDate(1);
    result.setUTCMonth(result.getUTCMonth() + months);
    const lastDay = new Date(
      Date.UTC(result.getUTCFullYear(), result.getUTCMonth() + 1, 0),
    ).getUTCDate();
    result.setUTCDate(Math.min(day, lastDay));
    return result;
  }

  function expirationForChoice(choice, now, customValue = "") {
    if (choice === "week") {
      return new Date(now.getTime() + 7 * 24 * 60 * 60 * 1000).toISOString();
    }
    if (choice === "month") return addUtcCalendarMonths(now, 1).toISOString();
    if (choice === "year") return addUtcCalendarMonths(now, 12).toISOString();

    const custom = new Date(customValue);
    const latest = addUtcCalendarMonths(now, 12);
    if (
      !customValue ||
      Number.isNaN(custom.getTime()) ||
      custom <= now ||
      custom > latest
    ) {
      throw new Error(
        "Choose a custom expiration after now and no more than one year away.",
      );
    }
    return custom.toISOString();
  }

  function contextLabel(context) {
    const role = context.role[0].toUpperCase() + context.role.slice(1);
    const scope = context.scope[0].toUpperCase() + context.scope.slice(1);
    return context.group_id
      ? `${role} · ${scope} · ${context.group_id}`
      : `${role} · ${scope}`;
  }

  function tokenIdentifier(token) {
    return token.name;
  }

  function tokenStatus(token, now = new Date()) {
    if (token.revoked_at) return "Revoked";
    if (new Date(token.expires_at) <= now) return "Expired";
    if (!token.assignment_is_active) return "Authorization removed";
    return "Active";
  }

  return Object.freeze({
    contextLabel,
    expirationForChoice,
    tokenIdentifier,
    tokenStatus,
  });
})();
if (typeof module !== "undefined" && module.exports) {
  module.exports = SabTokenManagement;
}

/*
 * This code is executed in a browser context and is responsible for rendering dynamic
 * elements of the Token Management interface, making token management API requests,
 * and handling user interactions.
 */
if (typeof document !== "undefined") (() => {
  const {contextLabel, expirationForChoice, tokenIdentifier, tokenStatus} =
    SabTokenManagement;
  const loading = document.querySelector("#loading");
  const signedOut = document.querySelector("#signed-out");
  const authenticated = document.querySelector("#authenticated");
  const statusMessage = document.querySelector("#status-message");
  const errorMessage = document.querySelector("#error-message");
  const createForm = document.querySelector("#create-token-form");
  const createTokenSubmit = createForm.querySelector('button[type="submit"]');
  const contextSelect = document.querySelector("#authorization-context");
  const customField = document.querySelector("#custom-expiration-field");
  const customInput = document.querySelector("#custom-expiration");
  const tokenList = document.querySelector("#token-list");
  const tokenTable = document.querySelector("#token-table");
  const noTokens = document.querySelector("#no-tokens");
  const noContextsHint = document.querySelector("#no-contexts");
  const hasContextsHint = document.querySelector("#has-contexts");
  const secretDialog = document.querySelector("#token-secret-dialog");
  const secretValue = document.querySelector("#new-token-secret");
  const revokeDialog = document.querySelector("#revoke-token-dialog");
  const revokeTarget = document.querySelector("#revoke-token-target");
  const contextHelpButton = document.querySelector("#context-help");
  const contextHelpDialog = document.querySelector("#context-help-dialog");
  const contextHelpClose = document.querySelector("#context-help-close");
  const copyTokenButton = document.querySelector("#copy-token");
  const closeTokenSecretButton = document.querySelector("#close-token-secret");
  const cancelRevokeButton = document.querySelector("#cancel-revoke");
  const confirmRevokeButton = document.querySelector("#confirm-revoke");
  const refreshTokensButton = document.querySelector("#refresh-tokens");
  let pendingRevokeId = null;

  function cookieValue(name) {
    const prefix = `${encodeURIComponent(name)}=`;
    const cookie = document.cookie
      .split("; ")
      .find((part) => part.startsWith(prefix));
    return cookie ? decodeURIComponent(cookie.slice(prefix.length)) : null;
  }

  function clearMessages() {
    statusMessage.textContent = "";
    errorMessage.textContent = "";
  }

  function showSignedOut() {
    if (revokeDialog.open) revokeDialog.close();
    pendingRevokeId = null;
    revokeTarget.textContent = "";
    loading.hidden = true;
    authenticated.hidden = true;
    signedOut.hidden = false;
    errorMessage.textContent =
      "Your token-management session is missing or expired. Sign in again.";
  }

  async function request(path, options = {}) {
    const headers = new Headers(options.headers || {});
    headers.set("Accept", "application/json");
    if (options.body !== undefined) {
      headers.set("Content-Type", "application/json");
    }
    if (options.method === "POST" || options.method === "DELETE") {
      const csrfToken = cookieValue("sab_token_management_csrf");
      if (!csrfToken) {
        showSignedOut();
        throw new Error("Token-management security state is unavailable.");
      }
      headers.set("X-CSRF-Token", csrfToken);
    }

    const response = await fetch(path, {
      ...options,
      credentials: "same-origin",
      headers,
    });
    if (response.status === 401) {
      showSignedOut();
      throw new Error("Your token-management session expired.");
    }
    const payload =
      response.status === 204 ? null : await response.json().catch(() => null);
    if (!response.ok) {
      const message =
        payload?.error?.message || "The token-management request failed.";
      throw new Error(message);
    }
    return payload;
  }

  function renderContexts(contexts) {
    contextSelect.replaceChildren();
    const hasContexts = contexts.length !== 0;
    if (hasContexts) {
      const placeholder = document.createElement("option");
      placeholder.value = "";
      placeholder.disabled = true;
      placeholder.selected = true;
      contextSelect.append(placeholder);
      for (const context of contexts) {
        const option = document.createElement("option");
        option.value = context.assignment_id;
        option.textContent = contextLabel(context);
        contextSelect.append(option);
      }
    }
    createTokenSubmit.disabled = !hasContexts;
    noContextsHint.hidden = hasContexts;
    hasContextsHint.hidden = !hasContexts;
  }

  function tableCell(text) {
    const cell = document.createElement("td");
    cell.textContent = text;
    return cell;
  }

  function renderTokens(tokens) {
    tokenList.replaceChildren();
    noTokens.hidden = tokens.length !== 0;
    tokenTable.hidden = tokens.length === 0;

    for (const token of tokens) {
      const row = document.createElement("tr");
      row.append(
        tableCell(tokenIdentifier(token)),
        tableCell(
          contextLabel({
            role: token.role,
            scope: token.scope,
            group_id: token.group_id,
          }),
        ),
        tableCell(new Date(token.created_at).toLocaleString()),
        tableCell(
          token.last_used_at
            ? new Date(token.last_used_at).toLocaleString()
            : "Never",
        ),
        tableCell(new Date(token.expires_at).toLocaleString()),
        tableCell(tokenStatus(token)),
      );
      const actionCell = document.createElement("td");
      if (!token.revoked_at) {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "link-button danger";
        button.textContent = "Revoke";
        button.setAttribute("aria-label", `Revoke ${tokenIdentifier(token)}`);
        button.addEventListener("click", () => {
          pendingRevokeId = token.token_id;
          revokeTarget.textContent = tokenIdentifier(token);
          revokeDialog.showModal();
        });
        actionCell.append(button);
      }
      row.append(actionCell);
      tokenList.append(row);
    }
  }

  async function refreshTokens() {
    const payload = await request("/tokens");
    renderTokens(payload.items);
  }

  function clearForm() {
    createForm.elements.name.value = "";
    createForm.elements.assignment_id.value = "";
    createForm.elements.expiration.value = "week";
    updateCustomExpiration();
  }

  function selectedExpiration() {
    return expirationForChoice(
      createForm.elements.expiration.value,
      new Date(),
      customInput.value,
    );
  }

  function updateCustomExpiration() {
    const isCustom = createForm.elements.expiration.value === "custom";
    customField.hidden = !isCustom;
    customInput.disabled = !isCustom;
    customInput.required = isCustom;
  }

  createForm.addEventListener("change", (event) => {
    if (event.target.name === "expiration") updateCustomExpiration();
  });

  createForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    clearMessages();
    createTokenSubmit.disabled = true;
    try {
      const created = await request("/tokens", {
        method: "POST",
        body: JSON.stringify({
          name: createForm.elements.name.value,
          assignment_id: contextSelect.value,
          expires_at: selectedExpiration(),
        }),
      });
      clearForm();
      secretValue.textContent = created.token;
      secretDialog.showModal();
      await refreshTokens();
    } catch (error) {
      errorMessage.textContent = error.message;
    } finally {
      createTokenSubmit.disabled = contextSelect.options.length === 0;
    }
  });

  contextHelpButton.addEventListener("click", () => {
    contextHelpDialog.showModal();
  });

  contextHelpClose.addEventListener("click", () => {
    contextHelpDialog.close();
  });

  copyTokenButton.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(secretValue.textContent);
      statusMessage.textContent = "Token copied. Store it securely now.";
    } catch {
      errorMessage.textContent =
        "The browser could not copy the token. Select and copy it manually.";
    }
  });

  function closeSecret() {
    secretDialog.close();
  }

  closeTokenSecretButton.addEventListener("click", closeSecret);
  secretDialog.addEventListener("close", () => {
    secretValue.textContent = "";
  });

  cancelRevokeButton.addEventListener("click", () => {
    pendingRevokeId = null;
    revokeTarget.textContent = "";
    revokeDialog.close();
  });

  confirmRevokeButton.addEventListener("click", async () => {
    if (!pendingRevokeId) return;
    clearMessages();
    confirmRevokeButton.disabled = true;
    try {
      await request(`/tokens/${encodeURIComponent(pendingRevokeId)}`, {
        method: "DELETE",
      });
      pendingRevokeId = null;
      revokeTarget.textContent = "";
      revokeDialog.close();
      statusMessage.textContent = "Token revoked.";
      await refreshTokens();
    } catch (error) {
      errorMessage.textContent = error.message;
    } finally {
      confirmRevokeButton.disabled = false;
    }
  });

  refreshTokensButton.addEventListener("click", async () => {
    clearMessages();
    try {
      await refreshTokens();
      statusMessage.textContent = "Token history refreshed.";
    } catch (error) {
      errorMessage.textContent = error.message;
    }
  });

  async function initialize() {
    clearMessages();
    try {
      const [contexts, tokens] = await Promise.all([
        request("/tokens/contexts"),
        request("/tokens"),
      ]);
      renderContexts(contexts.items);
      renderTokens(tokens.items);
      loading.hidden = true;
      signedOut.hidden = true;
      authenticated.hidden = false;
    } catch (error) {
      if (!signedOut.hidden) return;
      loading.hidden = true;
      errorMessage.textContent = error.message;
    }
  }

  window.addEventListener("pagehide", () => {
    secretValue.textContent = "";
  });

  initialize();
})();
