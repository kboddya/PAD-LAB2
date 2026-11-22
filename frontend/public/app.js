const state = {
  token: localStorage.getItem("shopflow_token") || null,
  user: JSON.parse(localStorage.getItem("shopflow_user") || "null"),
  products: [],
  cart: [],
  traces: [],
};

const $ = (id) => document.getElementById(id);

function esc(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function showToast(message, isError = false) {
  const el = document.createElement("div");
  el.className = `toast ${isError ? "error" : ""}`;
  el.textContent = message;
  $("toast-container").appendChild(el);
  setTimeout(() => el.remove(), 4000);
}

function recordTrace(method, path, status, reqId, rateRemaining) {
  state.traces.unshift({
    ts: new Date().toLocaleTimeString(),
    method,
    path,
    status,
    reqId: reqId || "-",
    rateRemaining: rateRemaining ?? "-",
  });
  state.traces = state.traces.slice(0, 20);
  renderTraceLog();
}

async function apiFetch(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (options.body && !headers["Content-Type"]) {
    headers["Content-Type"] = "application/json";
  }
  if (state.token) {
    headers["Authorization"] = `Bearer ${state.token}`;
  }

  const resp = await fetch(path, { ...options, headers });
  const reqId = resp.headers.get("X-Request-ID");
  const rateRem = resp.headers.get("X-RateLimit-Remaining");
  recordTrace(options.method || "GET", path, resp.status, reqId, rateRem);

  const text = await resp.text();
  let data = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = { raw: text };
  }

  if (!resp.ok) {
    const errMsg = data?.error?.message || data?.detail || `HTTP ${resp.status}`;
    throw new Error(errMsg);
  }
  return data;
}

function setSession(tokenResponse) {
  if (tokenResponse) {
    state.token = tokenResponse.access_token;
    state.user = tokenResponse.user;
    localStorage.setItem("shopflow_token", state.token);
    localStorage.setItem("shopflow_user", JSON.stringify(state.user));
  } else {
    state.token = null;
    state.user = null;
    localStorage.removeItem("shopflow_token");
    localStorage.removeItem("shopflow_user");
  }
  syncAuthUI();
}

function syncAuthUI() {
  const loggedIn = Boolean(state.token && state.user);
  $("user-pill").classList.toggle("hidden", !loggedIn);
  $("auth-card").classList.toggle("hidden", loggedIn);
  $("dashboard-guest").classList.toggle("hidden", loggedIn);
  $("dashboard-content").classList.toggle("hidden", !loggedIn);

  if (loggedIn) {
    $("user-pill-text").textContent = `${state.user.username} (${state.user.role})`;
    loadDashboard();
    loadNotifications();
  } else {
    $("notif-badge").textContent = "0";
    $("notifications-list").innerHTML = `<div class="empty-card">Sign in to view notifications.</div>`;
  }
}

async function checkClusterReady() {
  try {
    const resp = await fetch("/ready");
    const data = await resp.json();
    const checks = data.checks || {};
    const allOk = resp.ok && data.status === "ready";
    $("ready-dot").className = `dot ${allOk ? "dot-ok" : "dot-warn"}`;
    $("ready-label").textContent = allOk ? "All 6 services ready" : "Cluster degraded";
    renderClusterChecks(checks);
  } catch {
    $("ready-dot").className = "dot dot-err";
    $("ready-label").textContent = "Gateway unreachable";
  }
}

function renderClusterChecks(checks) {
  const container = $("cluster-checks");
  container.innerHTML = Object.entries(checks)
    .map(
      ([name, status]) => `
      <div class="check-card">
        <div class="muted">${esc(name)}</div>
        <strong><span class="dot ${status === "ok" ? "dot-ok" : "dot-err"}"></span> ${esc(status)}</strong>
      </div>`
    )
    .join("");
}

function renderTraceLog() {
  const el = $("trace-log");
  if (!state.traces.length) {
    el.innerHTML = `<div class="muted">No requests yet.</div>`;
    return;
  }
  el.innerHTML = state.traces
    .map(
      (t) =>
        `<div class="trace-entry">[${esc(t.ts)}] <strong>${esc(t.method)}</strong> ${esc(t.path)} → <strong>${esc(t.status)}</strong> | req_id=${esc(t.reqId)} | rate_rem=${esc(t.rateRemaining)}</div>`
    )
    .join("");
}

async function loadProducts() {
  const params = new URLSearchParams();
  const search = $("search-input").value.trim();
  if (search) params.set("search", search);
  if ($("instock-only").checked) params.set("in_stock", "true");
  const qs = params.toString() ? `?${params.toString()}` : "";

  try {
    const list = await apiFetch(`/api/products${qs}`);
    state.products = list;
    renderProducts();
  } catch (err) {
    showToast(`Failed to load catalog: ${err.message}`, true);
  }
}

function renderProducts() {
  const grid = $("products-grid");
  if (!state.products.length) {
    grid.innerHTML = `<div class="empty-card">No products match your filter.</div>`;
    return;
  }
  grid.innerHTML = state.products
    .map(
      (p) => `
      <article class="product-card">
        <div class="sku-tag">${esc(p.sku)} · ${esc(p.category)}</div>
        <h3>${esc(p.name)}</h3>
        <p class="muted">${esc(p.description || "No description")}</p>
        <div class="muted">Available: <strong>${Number(p.available_quantity)}</strong> (reserved: ${Number(p.reserved_quantity)})</div>
        <div class="price-row">
          <span class="price">$${Number(p.price).toFixed(2)}</span>
          <button class="secondary-btn" onclick="addToCart('${esc(p.id)}')" ${p.available_quantity <= 0 ? "disabled" : ""}>
            ${p.available_quantity > 0 ? "Add to Cart" : "Out of stock"}
          </button>
        </div>
      </article>`
    )
    .join("");
}

window.addToCart = (productId) => {
  const product = state.products.find((p) => p.id === productId);
  if (!product) return;
  const existing = state.cart.find((c) => c.product.id === productId);
  if (existing) {
    if (existing.quantity < product.available_quantity) {
      existing.quantity += 1;
    } else {
      showToast("Max available stock reached", true);
      return;
    }
  } else {
    state.cart.push({ product, quantity: 1 });
  }
  renderCart();
};

window.removeFromCart = (productId) => {
  state.cart = state.cart.filter((c) => c.product.id !== productId);
  renderCart();
};

function renderCart() {
  const totalCount = state.cart.reduce((acc, item) => acc + item.quantity, 0);
  const totalSum = state.cart.reduce((acc, item) => acc + item.quantity * item.product.price, 0);
  $("cart-badge").textContent = String(totalCount);
  $("cart-total").textContent = `$${totalSum.toFixed(2)}`;

  if (!state.cart.length) {
    $("cart-items").innerHTML = `<div class="muted">Cart is empty.</div>`;
    return;
  }
  $("cart-items").innerHTML = state.cart
    .map(
      (c) => `
      <div class="cart-row">
        <span>${esc(c.product.name)} × ${Number(c.quantity)}</span>
        <span>
          $${(c.quantity * c.product.price).toFixed(2)}
          <button class="danger-mini" onclick="removeFromCart('${esc(c.product.id)}')">×</button>
        </span>
      </div>`
    )
    .join("");
}

async function loadDashboard() {
  if (!state.token) return;
  try {
    const dash = await apiFetch("/api/dashboard");
    const banner = $("degraded-banner");
    if (dash.degraded_services && dash.degraded_services.length) {
      banner.textContent = `Partial degradation detected: ${dash.degraded_services.join(", ")}`;
      banner.classList.remove("hidden");
    } else {
      banner.classList.add("hidden");
    }

    $("dash-username").textContent = dash.profile?.full_name || state.user.username;
    $("dash-tier").textContent = `tier: ${dash.profile?.loyalty_tier || "-"}`;
    const orders = dash.recent_orders || [];
    $("dash-orders-count").textContent = String(orders.length);
    $("dash-unread").textContent = String(dash.unread_notifications ?? "-");
    $("dash-latency").textContent = `agg: ${dash.aggregation_latency_ms} ms`;

    if (dash.unread_notifications !== null && dash.unread_notifications !== undefined) {
      $("notif-badge").textContent = String(dash.unread_notifications);
    }

    const addrs = dash.profile?.addresses || [];
    $("addresses-list").innerHTML = addrs.length
      ? addrs
          .map(
            (a) =>
              `<li><strong>${esc(a.label)}${a.is_default ? " (default)" : ""}</strong>: ${esc(a.street)}, ${esc(a.city)}, ${esc(a.country)} (${esc(a.postal_code)})</li>`
          )
          .join("")
      : `<li class="muted">No saved addresses yet.</li>`;

    if (addrs.length && !$("checkout-address").value) {
      const def = addrs.find((a) => a.is_default) || addrs[0];
      $("checkout-address").value = `${def.street}, ${def.city}, ${def.country}`;
    }

    $("orders-list").innerHTML = orders.length
      ? orders
          .map(
            (o) => `
          <div class="order-card">
            <div class="price-row">
              <strong>Order ${esc(o.id.slice(0, 8))}...</strong>
              <span class="status-badge status-${esc(o.status)}">${esc(o.status)}</span>
            </div>
            <div class="muted">Total: $${Number(o.total_amount).toFixed(2)} ${esc(o.currency)} · ${esc(o.shipping_address)}</div>
            ${o.failure_reason ? `<div class="muted">Reason: ${esc(o.failure_reason)}</div>` : ""}
            <div class="price-row">
              <span class="muted">${o.items.map((i) => `${esc(i.product_name)} × ${Number(i.quantity)}`).join(", ")}</span>
              ${
                o.status === "paid" || o.status === "pending"
                  ? `<button class="danger-mini" onclick="cancelOrder('${esc(o.id)}')">Cancel Order</button>`
                  : ""
              }
            </div>
          </div>`
          )
          .join("")
      : `<div class="empty-card">No orders placed yet.</div>`;
  } catch (err) {
    showToast(`Dashboard error: ${err.message}`, true);
  }
}

window.cancelOrder = async (orderId) => {
  try {
    await apiFetch(`/api/orders/${orderId}/cancel`, { method: "POST" });
    showToast("Order cancelled & inventory restored via Saga");
    await Promise.all([loadDashboard(), loadProducts(), loadNotifications()]);
  } catch (err) {
    showToast(err.message, true);
  }
};

async function loadNotifications() {
  if (!state.token) return;
  const unreadOnly = $("unread-only").checked ? "?unread_only=true" : "";
  try {
    const items = await apiFetch(`/api/notifications${unreadOnly}`);
    const unread = items.filter((n) => !n.is_read).length;
    if (!$("unread-only").checked) {
      $("notif-badge").textContent = String(unread);
    }
    $("notifications-list").innerHTML = items.length
      ? items
          .map(
            (n) => `
          <div class="notif-card">
            <div class="price-row">
              <strong>${esc(n.title)} <span class="sku-tag">[${esc(n.category)}]</span></strong>
              ${
                n.is_read
                  ? `<span class="muted">Read</span>`
                  : `<button class="secondary-btn" onclick="markNotificationRead('${esc(n.id)}')">Mark read</button>`
              }
            </div>
            <div class="muted">${esc(n.message)}</div>
          </div>`
          )
          .join("")
      : `<div class="empty-card">No notifications.</div>`;
  } catch (err) {
    showToast(err.message, true);
  }
}

window.markNotificationRead = async (id) => {
  try {
    await apiFetch(`/api/notifications/${id}/read`, { method: "PATCH" });
    await Promise.all([loadNotifications(), loadDashboard()]);
  } catch (err) {
    showToast(err.message, true);
  }
};

function setupEvents() {
  document.querySelectorAll(".tab").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".tab").forEach((b) => b.classList.remove("active"));
      document.querySelectorAll(".tab-panel").forEach((p) => p.classList.remove("active"));
      btn.classList.add("active");
      $(`tab-${btn.dataset.tab}`).classList.add("active");
    });
  });

  $("mode-login").addEventListener("click", () => {
    $("mode-login").classList.add("active");
    $("mode-register").classList.remove("active");
    $("login-form").classList.remove("hidden");
    $("register-form").classList.add("hidden");
  });

  $("mode-register").addEventListener("click", () => {
    $("mode-register").classList.add("active");
    $("mode-login").classList.remove("active");
    $("register-form").classList.remove("hidden");
    $("login-form").classList.add("hidden");
  });

  $("login-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const form = new FormData(e.target);
    try {
      const data = await apiFetch("/api/auth/login", {
        method: "POST",
        body: JSON.stringify(Object.fromEntries(form)),
      });
      setSession(data);
      showToast(`Welcome back, ${data.user.username}!`);
    } catch (err) {
      showToast(err.message, true);
    }
  });

  $("register-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const form = new FormData(e.target);
    try {
      const data = await apiFetch("/api/auth/register", {
        method: "POST",
        body: JSON.stringify(Object.fromEntries(form)),
      });
      setSession(data);
      showToast(`Registered as ${data.user.role}!`);
    } catch (err) {
      showToast(err.message, true);
    }
  });

  $("btn-logout").addEventListener("click", async () => {
    try {
      await apiFetch("/api/auth/logout", { method: "POST" });
    } catch {
      // Clear local session even if token already expired
    }
    setSession(null);
    showToast("Logged out and JWT revoked");
  });

  $("checkout-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!state.token) {
      showToast("Please sign in before placing an order", true);
      return;
    }
    if (!state.cart.length) {
      showToast("Your cart is empty", true);
      return;
    }
    try {
      const order = await apiFetch("/api/orders", {
        method: "POST",
        body: JSON.stringify({
          items: state.cart.map((c) => ({ product_id: c.product.id, quantity: c.quantity })),
          shipping_address: $("checkout-address").value.trim(),
          simulate_payment_failure: $("simulate-failure").checked,
        }),
      });
      state.cart = [];
      renderCart();
      showToast(`Order ${order.status}: #${order.id.slice(0, 8)}`);
      await Promise.all([loadProducts(), loadDashboard(), loadNotifications()]);
    } catch (err) {
      showToast(`Checkout failed: ${err.message}`, true);
    }
  });

  $("address-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!state.user) return;
    const payload = Object.fromEntries(new FormData(e.target));
    payload.is_default = true;
    try {
      await apiFetch(`/api/users/${state.user.id}/addresses`, {
        method: "POST",
        body: JSON.stringify(payload),
      });
      e.target.reset();
      showToast("Shipping address added");
      loadDashboard();
    } catch (err) {
      showToast(err.message, true);
    }
  });

  $("create-product-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const raw = Object.fromEntries(new FormData(e.target));
    raw.price = Number(raw.price);
    raw.stock_quantity = Number(raw.stock_quantity);
    try {
      const created = await apiFetch("/api/products", {
        method: "POST",
        body: JSON.stringify(raw),
      });
      e.target.reset();
      showToast(`Created ${created.name} (${created.sku})`);
      loadProducts();
    } catch (err) {
      showToast(err.message, true);
    }
  });

  $("btn-refresh-products").addEventListener("click", loadProducts);
  $("search-input").addEventListener("input", () => loadProducts());
  $("instock-only").addEventListener("change", loadProducts);
  $("btn-refresh-dashboard").addEventListener("click", loadDashboard);
  $("btn-refresh-notifs").addEventListener("click", loadNotifications);
  $("unread-only").addEventListener("change", loadNotifications);
  $("btn-mark-all-read").addEventListener("click", async () => {
    if (!state.token) return;
    await apiFetch("/api/notifications/mark-all-read", { method: "POST" });
    await Promise.all([loadNotifications(), loadDashboard()]);
  });
  $("btn-check-ready").addEventListener("click", checkClusterReady);
  $("btn-reprobe").addEventListener("click", checkClusterReady);
}

setupEvents();
renderCart();
renderTraceLog();
syncAuthUI();
loadProducts();
checkClusterReady();
