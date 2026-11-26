const state = {
  token: localStorage.getItem("shopflow_token") || null,
  user: JSON.parse(localStorage.getItem("shopflow_user") || "null"),
  products: [],
  cart: [],
  serverCart: null,
  activeReviewProductId: null,
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
  state.traces = state.traces.slice(0, 25);
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
    state.serverCart = null;
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
    loadServerCart();
    loadTickets();
    if (state.user.role === "seller" || state.user.role === "admin") {
      loadSellerStudio();
    }
  } else {
    $("notif-badge").textContent = "0";
    $("notifications-list").innerHTML = `<div class="empty-card">Sign in to view notifications.</div>`;
    $("tickets-list").innerHTML = `<div class="empty-card">Sign in to manage support tickets.</div>`;
    renderCart();
  }
}

async function checkClusterReady() {
  try {
    const resp = await fetch("/ready");
    const data = await resp.json();
    const checks = data.checks || {};
    const count = Object.keys(checks).length;
    const allOk = resp.ok && data.status === "ready";
    $("ready-dot").className = `dot ${allOk ? "dot-ok" : "dot-warn"}`;
    $("ready-label").textContent = allOk ? `All ${count} services ready` : "Cluster degraded";
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
        <div class="price-row">
          <span class="sku-tag">${esc(p.sku)} · ${esc(p.category)}</span>
          <span class="rating-pill">★ ${Number(p.average_rating || 0).toFixed(1)} (${Number(p.review_count || 0)})</span>
        </div>
        <h3>${esc(p.name)}</h3>
        <p class="muted">${esc(p.description || "No description")}</p>
        <div class="muted">Available: <strong>${Number(p.available_quantity)}</strong> (reserved: ${Number(p.reserved_quantity)})</div>
        <div class="price-row">
          <span class="price">$${Number(p.price).toFixed(2)}</span>
          <div class="card-actions">
            <button class="secondary-btn" data-action="view-reviews" data-id="${esc(p.id)}" data-name="${esc(p.name)}">Reviews</button>
            <button class="secondary-btn" data-action="add-to-cart" data-id="${esc(p.id)}" ${p.available_quantity <= 0 ? "disabled" : ""}>
              ${p.available_quantity > 0 ? "Add" : "Out"}
            </button>
          </div>
        </div>
      </article>`
    )
    .join("");
}

async function openReviews(productId, productName) {
  state.activeReviewProductId = productId;
  $("reviews-title").textContent = `Verified Reviews · ${productName || productId}`;
  $("reviews-panel").classList.remove("hidden");
  await refreshActiveReviews();
}

async function refreshActiveReviews() {
  if (!state.activeReviewProductId) return;
  try {
    const [summary, reviews] = await Promise.all([
      apiFetch(`/api/reviews/product/${state.activeReviewProductId}/summary`),
      apiFetch(`/api/reviews?product_id=${encodeURIComponent(state.activeReviewProductId)}`),
    ]);
    $("reviews-summary").textContent = `Average: ${Number(summary.average_rating).toFixed(2)} / 5 across ${Number(summary.review_count)} verified review(s)`;
    $("reviews-list").innerHTML = reviews.length
      ? reviews
          .map(
            (r) => `
          <div class="order-card">
            <div class="price-row">
              <strong>${esc(r.title)} · ★ ${Number(r.rating)}/5</strong>
              <button class="secondary-btn" data-action="vote-helpful" data-id="${esc(r.id)}">Helpful (${Number(r.helpful_count)})</button>
            </div>
            <div class="muted">${esc(r.comment)}</div>
            ${r.seller_reply ? `<div class="muted mt-sm">Seller reply: ${esc(r.seller_reply)}</div>` : ""}
          </div>`
          )
          .join("")
      : `<div class="empty-card">No verified reviews yet.</div>`;
  } catch (err) {
    showToast(err.message, true);
  }
}

async function loadServerCart() {
  if (!state.token) return;
  try {
    state.serverCart = await apiFetch("/api/cart");
    renderCart();
  } catch (err) {
    showToast(`Cart sync error: ${err.message}`, true);
  }
}

async function refreshShippingQuote() {
  const method = $("shipping-method-select").value;
  let itemsCount = 1;
  let subtotal = 0;
  if (state.token && state.serverCart) {
    itemsCount = Math.max(1, Number(state.serverCart.items_count || 0));
    subtotal = Number(state.serverCart.subtotal || 0);
  } else {
    itemsCount = Math.max(1, state.cart.reduce((acc, c) => acc + c.quantity, 0));
    subtotal = state.cart.reduce((acc, c) => acc + c.quantity * c.product.price, 0);
  }
  try {
    const quote = await apiFetch("/api/shipping/quotes", {
      method: "POST",
      body: JSON.stringify({
        shipping_method: method,
        items_count: itemsCount,
        order_subtotal: subtotal,
      }),
    });
    $("shipping-quote-label").textContent = `Quote: $${Number(quote.cost).toFixed(2)} (${quote.carrier}, ~${quote.estimated_days}d)`;
  } catch {
    $("shipping-quote-label").textContent = "Shipping quote unavailable";
  }
}

window.addToCart = async (productId) => {
  if (state.token) {
    try {
      state.serverCart = await apiFetch("/api/cart/items", {
        method: "POST",
        body: JSON.stringify({ product_id: productId, quantity: 1 }),
      });
      renderCart();
      showToast("Added to server cart");
    } catch (err) {
      showToast(err.message, true);
    }
    return;
  }

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

window.removeFromCart = async (productId) => {
  if (state.token) {
    try {
      state.serverCart = await apiFetch(`/api/cart/items/${productId}`, { method: "DELETE" });
      renderCart();
    } catch (err) {
      showToast(err.message, true);
    }
    return;
  }
  state.cart = state.cart.filter((c) => c.product.id !== productId);
  renderCart();
};

function renderCart() {
  if (state.token && state.serverCart) {
    const c = state.serverCart;
    $("cart-badge").textContent = String(c.items_count || 0);
    $("cart-total").textContent =
      c.discount_amount > 0
        ? `$${Number(c.subtotal).toFixed(2)} → $${Number(c.estimated_total).toFixed(2)}`
        : `$${Number(c.subtotal || 0).toFixed(2)}`;

    if (c.coupon_code) {
      $("coupon-input").value = c.coupon_code;
      $("btn-remove-coupon").classList.remove("hidden");
      $("coupon-feedback").textContent = c.coupon_valid
        ? `Coupon ${c.coupon_code}: -$${Number(c.discount_amount).toFixed(2)}`
        : `Coupon ${c.coupon_code}: ${c.coupon_message || "not valid"}`;
    } else {
      $("btn-remove-coupon").classList.add("hidden");
      $("coupon-feedback").textContent = "";
    }

    if (!c.items || !c.items.length) {
      $("cart-items").innerHTML = `<div class="muted">Cart is empty.</div>`;
    } else {
      $("cart-items").innerHTML = c.items
        .map(
          (item) => `
          <div class="cart-row">
            <span>${esc(item.product_name)} × ${Number(item.quantity)}${item.price_changed ? " (price updated)" : ""}</span>
            <span>
              $${Number(item.line_total).toFixed(2)}
              <button class="danger-mini" data-action="remove-from-cart" data-id="${esc(item.product_id)}">×</button>
            </span>
          </div>`
        )
        .join("");
    }
    refreshShippingQuote();
    return;
  }

  const totalCount = state.cart.reduce((acc, item) => acc + item.quantity, 0);
  const totalSum = state.cart.reduce((acc, item) => acc + item.quantity * item.product.price, 0);
  $("cart-badge").textContent = String(totalCount);
  $("cart-total").textContent = `$${totalSum.toFixed(2)}`;
  $("btn-remove-coupon").classList.add("hidden");
  $("coupon-feedback").textContent = "";

  if (!state.cart.length) {
    $("cart-items").innerHTML = `<div class="muted">Cart is empty.</div>`;
  } else {
    $("cart-items").innerHTML = state.cart
      .map(
        (c) => `
        <div class="cart-row">
          <span>${esc(c.product.name)} × ${Number(c.quantity)}</span>
          <span>
            $${(c.quantity * c.product.price).toFixed(2)}
            <button class="danger-mini" data-action="remove-from-cart" data-id="${esc(c.product.id)}">×</button>
          </span>
        </div>`
      )
      .join("");
  }
  refreshShippingQuote();
}

async function loadDashboard() {
  if (!state.token) return;
  try {
    const [dash, promos] = await Promise.all([
      apiFetch("/api/dashboard"),
      apiFetch("/api/promotions").catch(() => []),
    ]);

    const banner = $("degraded-banner");
    if (dash.degraded_services && dash.degraded_services.length) {
      banner.textContent = `Partial degradation detected: ${dash.degraded_services.join(", ")}`;
      banner.classList.remove("hidden");
    } else {
      banner.classList.add("hidden");
    }

    $("dash-username").textContent = dash.profile?.full_name || state.user.username;
    $("dash-tier").textContent = `tier: ${dash.profile?.loyalty_tier || "-"}`;
    const ltv = dash.loyalty_summary;
    $("dash-ltv").textContent = ltv
      ? `Net Spend: $${Number(ltv.net_spend).toFixed(2)}${ltv.next_tier ? ` ($${Number(ltv.amount_to_next_tier).toFixed(2)} to ${ltv.next_tier})` : " (MAX)"}`
      : "LTV unavailable";

    const orders = dash.recent_orders || [];
    const shipments = dash.recent_shipments || [];
    const shipmentsByOrder = Object.fromEntries(shipments.map((s) => [s.order_id, s]));

    $("dash-orders-count").textContent = String(orders.length);
    $("dash-shipments-count").textContent = `${shipments.length} shipment(s)`;
    $("dash-unread").textContent = String(dash.unread_notifications ?? "-");
    $("dash-latency").textContent = `agg: ${dash.aggregation_latency_ms} ms`;

    if (dash.unread_notifications !== null && dash.unread_notifications !== undefined) {
      $("notif-badge").textContent = String(dash.unread_notifications);
    }

    $("promos-list").innerHTML = promos.length
      ? promos
          .map(
            (p) =>
              `<li><strong>${esc(p.code)}</strong> — ${p.discount_type === "percentage" ? `${Number(p.discount_value)}% off` : `$${Number(p.discount_value)} off`} (min $${Number(p.min_order_amount).toFixed(2)}, tier: ${esc(p.required_tier)}) · ${esc(p.description)}</li>`
          )
          .join("")
      : `<li class="muted">No active promotions right now.</li>`;

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
          .map((o) => {
            const ship = shipmentsByOrder[o.id];
            return `
          <div class="order-card">
            <div class="price-row">
              <strong>Order ${esc(o.id.slice(0, 8))}...</strong>
              <span class="status-badge status-${esc(o.status)}">${esc(o.status)}</span>
            </div>
            <div class="muted">Total: $${Number(o.total_amount).toFixed(2)} ${esc(o.currency)}${o.discount_amount > 0 ? ` (discount -$${Number(o.discount_amount).toFixed(2)})` : ""}${o.shipping_cost > 0 ? ` (+ship $${Number(o.shipping_cost).toFixed(2)})` : ""} · ${esc(o.shipping_address)}</div>
            ${ship ? `<div class="muted">Tracking: <strong>${esc(ship.tracking_number)}</strong> (${esc(ship.carrier)} · ${esc(ship.status)})</div>` : ""}
            ${o.failure_reason ? `<div class="muted">Reason: ${esc(o.failure_reason)}</div>` : ""}
            <div class="price-row">
              <span class="muted">${o.items.map((i) => `${esc(i.product_name)} × ${Number(i.quantity)}`).join(", ")}</span>
              ${
                o.status === "paid" || o.status === "pending"
                  ? `<button class="danger-mini" data-action="cancel-order" data-id="${esc(o.id)}">Cancel Order</button>`
                  : ""
              }
            </div>
          </div>`;
          })
          .join("")
      : `<div class="empty-card">No orders placed yet.</div>`;
  } catch (err) {
    showToast(`Dashboard error: ${err.message}`, true);
  }
}

async function loadTickets() {
  if (!state.token) return;
  try {
    const tickets = await apiFetch("/api/support/tickets");
    const isAdmin = state.user?.role === "admin";
    $("tickets-list").innerHTML = tickets.length
      ? tickets
          .map(
            (t) => `
          <div class="order-card">
            <div class="price-row">
              <strong>${esc(t.subject)} <span class="sku-tag">[${esc(t.category)} · ${esc(t.priority)}]</span></strong>
              <span class="status-badge status-${esc(t.status)}">${esc(t.status)}</span>
            </div>
            <div class="muted">Order: ${esc(t.order_id || "-")} · SLA: ${Number(t.sla_hours)}h${t.refund_amount ? ` · Refunded: $${Number(t.refund_amount).toFixed(2)}` : ""}</div>
            <div class="muted mt-sm">${(t.messages || []).map((m) => `<div><strong>${esc(m.author_role)}:</strong> ${esc(m.message)}</div>`).join("")}</div>
            ${
              isAdmin && t.order_id && t.status !== "resolved" && t.status !== "closed"
                ? `<div class="price-row"><button class="secondary-btn" data-action="approve-refund" data-id="${esc(t.id)}">Approve &amp; Refund</button></div>`
                : ""
            }
          </div>`
          )
          .join("")
      : `<div class="empty-card">No support tickets yet.</div>`;
  } catch (err) {
    showToast(err.message, true);
  }
}

async function loadSellerStudio() {
  if (!state.token || (state.user?.role !== "seller" && state.user?.role !== "admin")) return;
  try {
    const [overview, shipments] = await Promise.all([
      apiFetch("/api/analytics/overview"),
      apiFetch("/api/shipments?limit=20"),
    ]);
    $("seller-analytics").innerHTML = `
      <div class="stat-card"><span class="stat-label">GMV / Net Revenue</span><strong>$${Number(overview.gross_revenue).toFixed(2)}</strong><span class="muted">Net: $${Number(overview.net_revenue).toFixed(2)} (Refunds: $${Number(overview.refunds_total).toFixed(2)})</span></div>
      <div class="stat-card"><span class="stat-label">Orders Paid / Created</span><strong>${Number(overview.orders_paid)} / ${Number(overview.orders_created)}</strong><span class="muted">AOV: $${Number(overview.average_order_value).toFixed(2)}</span></div>
      <div class="stat-card"><span class="stat-label">Payment Success Rate</span><strong>${Number(overview.payment_success_rate_pct).toFixed(1)}%</strong><span class="muted">Delivered: ${Number(overview.shipments_delivered)}</span></div>
    `;

    $("studio-shipments-list").innerHTML = shipments.length
      ? shipments
          .map(
            (s) => `
          <div class="order-card">
            <div class="price-row">
              <strong>${esc(s.tracking_number)} · Order ${esc(s.order_id.slice(0, 8))}...</strong>
              <span class="status-badge status-${esc(s.status)}">${esc(s.status)}</span>
            </div>
            <div class="price-row">
              <span class="muted">${esc(s.carrier)} (${esc(s.shipping_method)})</span>
              <div class="card-actions">
                ${s.status === "preparing" ? `<button class="secondary-btn" data-action="advance-shipment" data-id="${esc(s.id)}" data-status="dispatched">Dispatch</button>` : ""}
                ${s.status === "dispatched" || s.status === "in_transit" ? `<button class="secondary-btn" data-action="advance-shipment" data-id="${esc(s.id)}" data-status="delivered">Mark Delivered</button>` : ""}
              </div>
            </div>
          </div>`
          )
          .join("")
      : `<div class="empty-card">No shipments in queue.</div>`;
  } catch (err) {
    showToast(err.message, true);
  }
}

window.cancelOrder = async (orderId) => {
  try {
    await apiFetch(`/api/orders/${orderId}/cancel`, { method: "POST" });
    showToast("Order cancelled & inventory/coupon restored via Saga");
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
                  : `<button class="secondary-btn" data-action="mark-notif-read" data-id="${esc(n.id)}">Mark read</button>`
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
  document.addEventListener("click", async (e) => {
    const btn = e.target.closest("[data-action]");
    if (!btn) return;
    const { action, id, name, status } = btn.dataset;
    if (action === "add-to-cart") window.addToCart(id);
    else if (action === "remove-from-cart") window.removeFromCart(id);
    else if (action === "cancel-order") window.cancelOrder(id);
    else if (action === "mark-notif-read") window.markNotificationRead(id);
    else if (action === "view-reviews") openReviews(id, name);
    else if (action === "vote-helpful") {
      try {
        await apiFetch(`/api/reviews/${id}/helpful`, { method: "POST" });
        await refreshActiveReviews();
      } catch (err) {
        showToast(err.message, true);
      }
    } else if (action === "advance-shipment") {
      try {
        await apiFetch(`/api/shipments/${id}/status`, {
          method: "POST",
          body: JSON.stringify({ status, location: "Chisinau Hub" }),
        });
        showToast(`Shipment marked as ${status}`);
        await Promise.all([loadSellerStudio(), loadDashboard(), loadNotifications()]);
      } catch (err) {
        showToast(err.message, true);
      }
    } else if (action === "approve-refund") {
      try {
        await apiFetch(`/api/support/tickets/${id}/approve-refund`, { method: "POST", body: JSON.stringify({}) });
        showToast("Refund approved and processed");
        await Promise.all([loadTickets(), loadDashboard(), loadNotifications()]);
      } catch (err) {
        showToast(err.message, true);
      }
    }
  });

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
      showToast(`Registered as ${data.user.role}! Welcome coupon provisioned.`);
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

  $("btn-apply-coupon").addEventListener("click", async () => {
    if (!state.token) {
      showToast("Sign in to apply a coupon", true);
      return;
    }
    const code = $("coupon-input").value.trim();
    if (!code) return;
    try {
      state.serverCart = await apiFetch("/api/cart/coupon", {
        method: "POST",
        body: JSON.stringify({ code }),
      });
      renderCart();
      showToast(`Coupon ${code.toUpperCase()} applied`);
    } catch (err) {
      showToast(err.message, true);
    }
  });

  $("btn-remove-coupon").addEventListener("click", async () => {
    if (!state.token) return;
    try {
      state.serverCart = await apiFetch("/api/cart/coupon", { method: "DELETE" });
      $("coupon-input").value = "";
      renderCart();
    } catch (err) {
      showToast(err.message, true);
    }
  });

  $("shipping-method-select").addEventListener("change", refreshShippingQuote);

  $("checkout-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!state.token) {
      showToast("Please sign in before placing an order", true);
      return;
    }
    const hasServerItems = Boolean(state.serverCart && state.serverCart.items && state.serverCart.items.length);
    if (!hasServerItems) {
      showToast("Your cart is empty", true);
      return;
    }
    try {
      const order = await apiFetch("/api/cart/checkout", {
        method: "POST",
        body: JSON.stringify({
          shipping_address: $("checkout-address").value.trim(),
          shipping_method: $("shipping-method-select").value,
          simulate_payment_failure: $("simulate-failure").checked,
        }),
      });
      showToast(`Order ${order.status}: #${order.id.slice(0, 8)}`);
      await Promise.all([loadServerCart(), loadProducts(), loadDashboard(), loadNotifications()]);
    } catch (err) {
      showToast(`Checkout failed: ${err.message}`, true);
    }
  });

  $("review-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!state.activeReviewProductId) return;
    const raw = Object.fromEntries(new FormData(e.target));
    try {
      await apiFetch("/api/reviews", {
        method: "POST",
        body: JSON.stringify({
          product_id: state.activeReviewProductId,
          rating: Number(raw.rating),
          title: raw.title,
          comment: raw.comment,
        }),
      });
      e.target.reset();
      showToast("Verified review published");
      await Promise.all([refreshActiveReviews(), loadProducts()]);
    } catch (err) {
      showToast(err.message, true);
    }
  });

  $("btn-close-reviews").addEventListener("click", () => {
    $("reviews-panel").classList.add("hidden");
    state.activeReviewProductId = null;
  });

  $("create-ticket-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!state.token) {
      showToast("Sign in to open a support ticket", true);
      return;
    }
    const raw = Object.fromEntries(new FormData(e.target));
    if (!raw.order_id) delete raw.order_id;
    try {
      const created = await apiFetch("/api/support/tickets", {
        method: "POST",
        body: JSON.stringify(raw),
      });
      e.target.reset();
      showToast(`Ticket opened (priority: ${created.priority}, SLA: ${created.sla_hours}h)`);
      loadTickets();
    } catch (err) {
      showToast(err.message, true);
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

  $("create-promo-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const raw = Object.fromEntries(new FormData(e.target));
    raw.discount_value = Number(raw.discount_value);
    raw.min_order_amount = Number(raw.min_order_amount || 0);
    try {
      const created = await apiFetch("/api/promotions", {
        method: "POST",
        body: JSON.stringify(raw),
      });
      e.target.reset();
      showToast(`Promotion ${created.code} created`);
      loadDashboard();
    } catch (err) {
      showToast(err.message, true);
    }
  });

  $("btn-refresh-products").addEventListener("click", loadProducts);
  $("search-input").addEventListener("input", () => loadProducts());
  $("instock-only").addEventListener("change", loadProducts);
  $("btn-refresh-dashboard").addEventListener("click", loadDashboard);
  $("btn-refresh-notifs").addEventListener("click", loadNotifications);
  $("btn-refresh-tickets").addEventListener("click", loadTickets);
  $("btn-refresh-studio").addEventListener("click", loadSellerStudio);
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
