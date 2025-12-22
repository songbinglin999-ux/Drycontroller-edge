<script lang="ts">
  import { onMount } from "svelte";
  import {
    getSystemStatus,
    getSystemMetrics,
    connectSystemStream,
    fetchMonitorConfig,
    updateMonitorConfig,
    restartService,
    toggleSelfHeal,
    fetchAlerts,
    type ServiceStatus,
    type SystemStatusPayload,
    type SystemMetricsPayload,
    type MonitorConfig,
    type AlertItem
  } from "./lib/api";

  import Timeline from "./components/Timeline.svelte";
  import Resources from "./components/Resources.svelte";
  import ContainerModal from "./components/ContainerModal.svelte";

  const REFRESH_INTERVAL = 15000;

  let line = "L1";
  let loadingStatus = true;
  let loadingMetrics = true;
  let statusError = "";
  let metricError = "";

  let statusPayload: SystemStatusPayload | null = null;
  let metricsPayload: SystemMetricsPayload | null = null;
  let services: ServiceStatus[] = [];
  let events: any[] = [];
  let monitorConfig: MonitorConfig | null = null;
  let alerts: AlertItem[] = [];
  let alertsLoading = true;
  let alertsError = "";
  let alertSeverity = "";
  let alertState = "active";
  
  // Config State
  let showConfig = false;
  let configForm = {
    interval: "",
    failure_threshold: "",
    recovery_observe: "",
    max_restart_per_hour: "",
    self_heal_enabled: true
  };
  let configLoading = true;
  let configError = "";
  let configSuccess = "";

  // Container Modal State
  let selectedServiceDetail: ServiceStatus | null = null;

  // Action States
  let actionLoading: Record<string, boolean> = {};
  let actionError = "";
  let actionSuccess = "";

  let disconnectStream: (() => void) | null = null;
  let refreshTimer: ReturnType<typeof setInterval> | null = null;
  let alertsTimer: ReturnType<typeof setInterval> | null = null;
  let currentTime = new Date().toLocaleTimeString();

  function updateClock() {
    currentTime = new Date().toLocaleTimeString();
  }

  function formatTime(ts?: string) {
    if (!ts) return "—";
    const d = new Date(ts);
    return `${d.toLocaleDateString()} ${d.toLocaleTimeString()}`;
  }

  function normalizeServices(payload: SystemStatusPayload | null): ServiceStatus[] {
    const raw = payload?.services;
    if (!raw) return [];
    if (Array.isArray(raw)) return raw;
    return Object.entries(raw).map(([name, info]) => ({
      name,
      status: info.status,
      latency_ms: info.latency_ms,
      container_state: info.container_state,
      self_heal: (info as any).self_heal
    }));
  }

  async function loadStatus() {
    loadingStatus = true;
    statusError = "";
    try {
        const payload = await getSystemStatus(line);
        statusPayload = payload;
        services = normalizeServices(payload);
    } catch (error) {
      statusError = (error as Error).message;
    } finally {
      loadingStatus = false;
    }
  }

  async function loadMetrics() {
    loadingMetrics = true;
    metricError = "";
    try {
      metricsPayload = await getSystemMetrics(line);
    } catch (error) {
      metricError = (error as Error).message;
    } finally {
      loadingMetrics = false;
    }
  }

  async function loadMonitorConfig() {
    configLoading = true;
    configError = "";
    try {
      const data = await fetchMonitorConfig();
      monitorConfig = data;
      const servicesCfg = data.services ?? {};
      configForm = {
        interval: servicesCfg.interval?.toString() ?? "",
        failure_threshold: servicesCfg.failure_threshold?.toString() ?? "",
        recovery_observe: servicesCfg.recovery_observe?.toString() ?? "",
        max_restart_per_hour: servicesCfg.max_restart_per_hour?.toString() ?? "",
        self_heal_enabled: servicesCfg.self_heal?.enabled ?? true
      };
    } catch (error) {
      configError = (error as Error).message;
    } finally {
      configLoading = false;
    }
  }

  async function loadAlerts() {
    alertsLoading = true;
    alertsError = "";
    try {
      const resp = await fetchAlerts({
        severity: alertSeverity || undefined,
        state: alertState || "active"
      });
      alerts = resp.alerts || [];
    } catch (error) {
      alertsError = (error as Error).message;
    } finally {
      alertsLoading = false;
    }
  }

  function startPolling() {
    if (refreshTimer) {
      clearInterval(refreshTimer);
    }
    refreshTimer = setInterval(() => {
      loadStatus();
      loadMetrics();
    }, REFRESH_INTERVAL);
    if (alertsTimer) {
      clearInterval(alertsTimer);
    }
    alertsTimer = setInterval(() => {
      loadAlerts();
    }, REFRESH_INTERVAL);
  }

  function startStream() {
    if (disconnectStream) {
      disconnectStream();
    }
    disconnectStream = connectSystemStream(line, (payload) => {
      events = [payload, ...events].slice(0, 20);
    });
  }

  function handleLineChange(event: Event) {
    line = (event.target as HTMLSelectElement).value;
    loadStatus();
    loadMetrics();
    startStream();
  }

  async function saveMonitorConfig() {
    configError = "";
    configSuccess = "";
    try {
      await updateMonitorConfig({
        interval: configForm.interval ? Number(configForm.interval) : undefined,
        failure_threshold: configForm.failure_threshold ? Number(configForm.failure_threshold) : undefined,
        recovery_observe: configForm.recovery_observe ? Number(configForm.recovery_observe) : undefined,
        max_restart_per_hour: configForm.max_restart_per_hour ? Number(configForm.max_restart_per_hour) : undefined,
        self_heal_enabled: configForm.self_heal_enabled
      });
      configSuccess = "配置已更新";
      setTimeout(() => { configSuccess = ""; showConfig = false; }, 1500);
      loadMonitorConfig();
    } catch (error) {
      configError = (error as Error).message;
    }
  }

  async function handleRestart(serviceName: string, e: Event) {
    e.stopPropagation(); // 防止触发卡片点击
    actionError = "";
    actionSuccess = "";
    actionLoading[serviceName] = true;
    try {
      await restartService(serviceName);
      actionSuccess = `已触发 ${serviceName} 重启`;
      setTimeout(() => actionSuccess = "", 3000);
    } catch (error) {
      actionError = (error as Error).message;
      setTimeout(() => actionError = "", 3000);
    } finally {
        actionLoading[serviceName] = false;
    }
  }

  async function handleSelfHealToggle(serviceName: string, enabled: boolean, e: Event) {
    e.stopPropagation(); // 防止触发卡片点击
    actionError = "";
    actionSuccess = "";
    actionLoading[serviceName] = true;
    try {
      await toggleSelfHeal(serviceName, enabled);
      actionSuccess = `${enabled ? "已开启" : "已关闭"} ${serviceName} 自愈`;
      loadStatus();
      setTimeout(() => actionSuccess = "", 3000);
    } catch (error) {
      actionError = (error as Error).message;
      setTimeout(() => actionError = "", 3000);
    } finally {
        actionLoading[serviceName] = false;
    }
  }

  onMount(() => {
    loadStatus();
    loadMetrics();
    loadMonitorConfig();
    loadAlerts();
    startPolling();
    startStream();
    
    const clockTimer = setInterval(updateClock, 1000);

    return () => {
      clearInterval(clockTimer);
      if (refreshTimer) {
        clearInterval(refreshTimer);
      }
      if (alertsTimer) {
        clearInterval(alertsTimer);
      }
      if (disconnectStream) {
        disconnectStream();
      }
    };
  });
</script>

<div class="app-shell">
  <!-- Header -->
  <header>
    <div class="brand">
      <div class="logo-icon">
        <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="2" width="20" height="8" rx="2" ry="2"></rect><rect x="2" y="14" width="20" height="8" rx="2" ry="2"></rect><line x1="6" y1="6" x2="6.01" y2="6"></line><line x1="6" y1="18" x2="6.01" y2="18"></line></svg>
      </div>
      <div>
        <h1>Edge Ops</h1>
        <span class="subtitle">智能监控看板</span>
      </div>
    </div>
    
    <div class="controls">
      <div class="clock-display">
        {currentTime}
      </div>
      <div class="select-wrapper">
        <select on:change={handleLineChange} bind:value={line}>
          <option value="L1">产线 L1</option>
          <option value="L2">产线 L2</option>
          <option value="">全部产线</option>
        </select>
      </div>
      <button class="icon-btn" on:click={() => showConfig = !showConfig} title="全局配置">
        <svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"></circle><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"></path></svg>
      </button>
    </div>
  </header>

  <!-- Main Content -->
  <main>
    {#if actionError || actionSuccess}
      <div class="toast {actionError ? 'error' : 'success'}">
        {actionError || actionSuccess}
      </div>
    {/if}

    <!-- Metrics Row (Replaced by Resources Component) -->
    <Resources metrics={metricsPayload} />

    <!-- Dashboard Grid -->
    <div class="dashboard-grid">
      <div class="main-column">
        <!-- Service List -->
        <section class="service-section">
          <div class="section-header">
            <div class="header-left">
              <h2>服务监控</h2>
              <span class="badge">{services.length}</span>
            </div>
            <div class="status-legend">
              <div class="legend-item">
                <span class="status-dot status-healthy"></span>
                <span>正常</span>
              </div>
              <div class="legend-item">
                <span class="status-dot status-degraded"></span>
                <span>异常</span>
              </div>
              <div class="legend-item">
                <span class="status-dot status-down"></span>
                <span>离线</span>
              </div>
            </div>
          </div>
          {#if loadingStatus && services.length === 0}
            <div class="loading-state">加载服务状态...</div>
          {:else if services.length === 0}
            <div class="empty-state">暂无服务</div>
          {:else}
            <div class="service-grid">
              {#each services as service}
                <!-- svelte-ignore a11y-click-events-have-key-events -->
                <!-- svelte-ignore a11y-no-static-element-interactions -->
                <div class="service-card" on:click={() => selectedServiceDetail = service}>
                  <div class="card-header">
                    <h3 title={service.name}>{service.name}</h3>
                    <span class="status-dot status-{service.status}" title={service.status}></span>
                  </div>
                  <div class="card-body">
                    <div class="stat-row">
                      <span class="stat-label">HTTP 延迟</span>
                      <span class="stat-value">{service.latency_ms ? `${service.latency_ms}ms` : "—"}</span>
                    </div>
                    <div class="stat-row">
                      <span class="stat-label">最近重启 (1h)</span>
                      <span class="stat-value">{service.self_heal?.state?.restart_count_hour ?? 0}</span>
                    </div>
                    {#if service.self_heal?.state?.last_action}
                      <div class="stat-row">
                        <span class="stat-label">最近自愈</span>
                        <span class="stat-value" style="font-size: 0.75rem">
                          {service.self_heal.state.last_action} 
                          ({new Date((service.self_heal.state.last_action_at ?? 0) * 1000).toLocaleTimeString()})
                        </span>
                      </div>
                    {/if}
                  </div>
                  <div class="card-actions">
                    <button 
                      class="action-btn restart" 
                      on:click={(e) => handleRestart(service.name, e)}
                      disabled={actionLoading[service.name]}
                      title="重启服务"
                    >
                      <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="23 4 23 10 17 10"></polyline><polyline points="1 20 1 14 7 14"></polyline><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"></path></svg>
                      重启
                    </button>
                    
                    {#if service.self_heal?.state?.self_heal_enabled ?? true}
                      <button 
                        class="action-btn pause" 
                        on:click={(e) => handleSelfHealToggle(service.name, false, e)}
                        disabled={actionLoading[service.name]}
                        title="暂停自愈"
                      >
                        <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="6" y="4" width="4" height="16"></rect><rect x="14" y="4" width="4" height="16"></rect></svg>
                        暂停
                      </button>
                    {:else}
                      <button 
                        class="action-btn resume" 
                        on:click={(e) => handleSelfHealToggle(service.name, true, e)}
                        disabled={actionLoading[service.name]}
                        title="恢复自愈"
                      >
                        <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="5 3 19 12 5 21 5 3"></polygon></svg>
                        恢复
                      </button>
                    {/if}
                  </div>
                </div>
              {/each}
            </div>
          {/if}
        </section>

        <!-- Timeline Component -->
        <Timeline />
      </div>

      <div class="side-column">
        <!-- Alerts Panel -->
        <section class="events-section">
          <div class="section-header">
            <h2>告警</h2>
            <div class="filters">
              <select bind:value={alertSeverity} on:change={loadAlerts}>
                <option value="">全部严重性</option>
                <option value="critical">critical</option>
                <option value="warning">warning</option>
              </select>
              <select bind:value={alertState} on:change={loadAlerts}>
                <option value="active">active</option>
                <option value="silenced">silenced</option>
                <option value="inhibited">inhibited</option>
              </select>
            </div>
          </div>
          {#if alertsLoading}
            <div class="loading-state small">加载告警...</div>
          {:else if alertsError}
            <div class="error-msg small">{alertsError}</div>
          {:else if alerts.length === 0}
            <div class="empty-state small">暂无告警</div>
          {:else}
            <div class="alerts-list">
              {#each alerts as alert}
                <div class="alert-item">
                  <div class="alert-header">
                    <span class="badge badge-{alert.labels?.severity || 'unknown'}">
                      {alert.labels?.severity || "unknown"}
                    </span>
                    <span class="alert-service">{alert.labels?.service || alert.labels?.job || "n/a"}</span>
                  </div>
                  <div class="alert-summary">
                    {alert.annotations?.summary || alert.labels?.alertname || "告警"}
                  </div>
                  {#if alert.annotations?.description}
                    <div class="alert-desc">{alert.annotations.description}</div>
                  {/if}
                  <div class="alert-footer">
                    <span>起始：{formatTime(alert.startsAt)}</span>
                    <span>状态：{alert.status?.state || "active"}</span>
                  </div>
                </div>
              {/each}
            </div>
          {/if}
        </section>

        <!-- Events Feed -->
        <section class="events-section">
          <div class="section-header">
            <h2>自愈事件流</h2>
          </div>
          <div class="events-container">
            {#if events.length === 0}
              <div class="empty-state small">暂无事件</div>
            {:else}
              <div class="events-list">
                {#each events as event}
                  <div class="event-item">
                    <div class="event-time">{new Date((event.timestamp ?? Date.now() / 1000) * 1000).toLocaleTimeString()}</div>
                    <div class="event-content">
                      <div class="event-title">
                        <strong>{event.service ?? event.data?.service ?? "System"}</strong>
                        <span class="event-action">{event.decision?.action ?? event.action ?? "Info"}</span>
                      </div>
                      <div class="event-reason" title={event.decision?.reason ?? event.error ?? "—"}>
                        {event.decision?.reason ?? (event.error ? `错误: ${event.error}` : "—")}
                      </div>
                    </div>
                    <div class="event-status {event.success ? 'success' : 'fail'}" title={event.success ? '成功' : '失败'}></div>
                  </div>
                {/each}
              </div>
            {/if}
          </div>
        </section>
      </div>
    </div>
  </main>

  <!-- Config Modal -->
  {#if showConfig}
    <!-- svelte-ignore a11y-click-events-have-key-events -->
    <!-- svelte-ignore a11y-no-static-element-interactions -->
    <div class="modal-overlay" on:click={() => showConfig = false}>
      <div class="modal" on:click|stopPropagation>
        <div class="modal-header">
          <h3>全局自愈配置</h3>
          <button class="close-btn" on:click={() => showConfig = false}>×</button>
        </div>
        <div class="modal-body">
          {#if configLoading}
            <p>加载配置中...</p>
          {:else}
            <div class="form-grid">
              <label>
                <span>探活间隔 (s)</span>
                <input type="number" bind:value={configForm.interval} min="1" />
              </label>
              <label>
                <span>失败阈值</span>
                <input type="number" bind:value={configForm.failure_threshold} min="1" />
              </label>
              <label>
                <span>观察窗口 (s)</span>
                <input type="number" bind:value={configForm.recovery_observe} min="1" />
              </label>
              <label>
                <span>时限重启数</span>
                <input type="number" bind:value={configForm.max_restart_per_hour} min="0" />
              </label>
            </div>
            <div class="form-row checkbox-row">
              <input type="checkbox" id="globalHeal" bind:checked={configForm.self_heal_enabled} />
              <label for="globalHeal">启用全局自愈功能</label>
            </div>
            
            {#if configError}
              <div class="error-msg">{configError}</div>
            {/if}
            {#if configSuccess}
              <div class="success-msg">{configSuccess}</div>
            {/if}
          {/if}
        </div>
        <div class="modal-footer">
          <button class="btn-secondary" on:click={() => showConfig = false}>取消</button>
          <button class="btn-primary" on:click={saveMonitorConfig}>保存更改</button>
        </div>
      </div>
    </div>
  {/if}

  {#if selectedServiceDetail}
    <ContainerModal 
      service={selectedServiceDetail} 
      show={true} 
      onClose={() => selectedServiceDetail = null} 
    />
  {/if}
</div>

<style>
  .dashboard-grid {
    display: grid;
    grid-template-columns: 2fr 1fr;
    gap: 1.5rem;
    align-items: start;
  }

  .main-column {
    display: flex;
    flex-direction: column;
    gap: 1.5rem;
  }

  .side-column {
    position: sticky;
    top: 100px;
  }

  /* Responsive adjustments */
  @media (max-width: 1024px) {
    .dashboard-grid {
      grid-template-columns: 1fr;
    }
    
    .side-column {
      position: static;
    }
  }

  .alerts-list {
    display: flex;
    flex-direction: column;
    gap: 0.75rem;
  }
  .alert-item {
    background: #1f2937;
    border: 1px solid #2f3b4f;
    border-radius: 10px;
    padding: 0.75rem 1rem;
    box-shadow: 0 1px 2px rgba(0,0,0,0.15);
  }
  .alert-header {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    margin-bottom: 0.35rem;
  }
  .badge {
    display: inline-flex;
    align-items: center;
    padding: 0.1rem 0.5rem;
    border-radius: 6px;
    font-size: 0.75rem;
    text-transform: uppercase;
    letter-spacing: 0.02em;
  }
  .badge-critical { background: #7f1d1d; color: #fca5a5; }
  .badge-warning { background: #78350f; color: #fcd34d; }
  .badge-unknown { background: #374151; color: #e5e7eb; }
  .alert-service {
    color: #d1d5db;
    font-weight: 600;
  }
  .alert-summary { color: #e5e7eb; font-weight: 500; }
  .alert-desc { color: #9ca3af; font-size: 0.9rem; margin-top: 0.25rem; }
  .alert-footer {
    margin-top: 0.35rem;
    font-size: 0.8rem;
    color: #9ca3af;
    display: flex;
    justify-content: space-between;
  }
  .filters {
    display: flex;
    gap: 0.5rem;
  }
  .filters select {
    background: #111827;
    border: 1px solid #374151;
    color: #e5e7eb;
    padding: 0.25rem 0.5rem;
    border-radius: 6px;
  }
</style>