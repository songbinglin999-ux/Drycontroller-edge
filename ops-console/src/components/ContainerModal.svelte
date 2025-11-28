<script lang="ts">
  import { onMount } from "svelte";
  import { restartService, fetchContainerLogs, type ServiceStatus } from "../lib/api";

  export let service: ServiceStatus;
  export let show = false;
  export let onClose: () => void;

  let activeTab = "info"; // info, logs, stats
  let logs: {ts: string, msg: string, level: string}[] = [];
  let loadingLogs = false;
  let loadingAction = false;
  let error = "";
  let logRefreshTimer: ReturnType<typeof setInterval> | null = null;
  let autoScroll = true;

  async function loadLogs() {
    if (loadingLogs) return;
    loadingLogs = true;
    try {
      const containerName = service.container_state ? service.name : service.name; // Use service name as container name usually matches
      const resp = await fetchContainerLogs(containerName);
      // Determine log level based on content if simple parser didn't catch it
      logs = resp.data.map(log => {
        let level = "INFO";
        if (log.msg.includes("ERROR") || log.msg.includes("Error") || log.msg.includes("Exception")) level = "ERROR";
        else if (log.msg.includes("WARN")) level = "WARN";
        return { ...log, level };
      });
      
      if (autoScroll) {
        setTimeout(() => {
          const viewer = document.querySelector('.logs-viewer');
          if (viewer) viewer.scrollTop = viewer.scrollHeight;
        }, 10);
      }
    } catch (e) {
      console.error("Failed to load logs", e);
      logs = [{ ts: new Date().toISOString(), level: "ERROR", msg: `无法加载日志: ${(e as Error).message}` }];
    } finally {
      loadingLogs = false;
    }
  }

  async function handleAction(action: string) {
    loadingAction = true;
    error = "";
    try {
      if (action === "restart") {
        await restartService(service.name);
      }
      // Add stop/start later
    } catch (e) {
      error = (e as Error).message;
    } finally {
      loadingAction = false;
    }
  }

  $: if (show && activeTab === "logs") {
    loadLogs();
    if (!logRefreshTimer) {
        logRefreshTimer = setInterval(loadLogs, 5000);
    }
  } else {
    if (logRefreshTimer) {
        clearInterval(logRefreshTimer);
        logRefreshTimer = null;
    }
  }

  onMount(() => {
    return () => {
        if (logRefreshTimer) clearInterval(logRefreshTimer);
    };
  });
</script>

{#if show}
  <div class="modal-overlay" on:click={onClose}>
    <div class="modal" on:click|stopPropagation>
      <div class="modal-header">
        <h3>容器管理: {service.name}</h3>
        <button class="close-btn" on:click={onClose}>×</button>
      </div>
      
      <div class="tabs">
        <button class:active={activeTab === 'info'} on:click={() => activeTab = 'info'}>概览</button>
        <button class:active={activeTab === 'logs'} on:click={() => activeTab = 'logs'}>实时日志</button>
        <button class:active={activeTab === 'stats'} on:click={() => activeTab = 'stats'}>资源监控</button>
      </div>

      <div class="modal-body">
        {#if activeTab === 'info'}
          <div class="info-panel">
            <div class="status-row">
              <span class="label">运行状态:</span>
              <span class="value status-{service.status}">{service.status}</span>
            </div>
            <div class="status-row">
              <span class="label">容器状态:</span>
              <span class="value">{service.container_state ?? "未知"}</span>
            </div>
            
            <div class="actions-group">
              <h4>快捷操作</h4>
              <div class="buttons">
                <button class="btn-action restart" on:click={() => handleAction('restart')} disabled={loadingAction}>
                  重启容器
                </button>
                <!-- Stop/Start placeholders -->
                <button class="btn-action stop" disabled title="暂未实现">停止</button>
              </div>
              {#if error}
                <p class="error-msg">{error}</p>
              {/if}
            </div>
          </div>

        {:else if activeTab === 'logs'}
          <div class="logs-panel">
            <div class="logs-controls">
              <label><input type="checkbox" bind:checked={autoScroll}> 自动滚动</label>
              <button class="btn-sm" on:click={loadLogs} disabled={loadingLogs}>刷新</button>
            </div>
            <div class="logs-viewer">
              {#if logs.length === 0 && !loadingLogs}
                <div class="empty-logs">暂无日志数据</div>
              {:else}
                {#each logs as log}
                  <div class="log-line">
                    <span class="ts">{new Date(log.ts).toLocaleTimeString()}</span>
                    <span class="level {log.level.toLowerCase()}">{log.level}</span>
                    <span class="msg">{log.msg}</span>
                  </div>
                {/each}
              {/if}
            </div>
          </div>

        {:else if activeTab === 'stats'}
          <div class="stats-panel">
            <p>容器级 CPU/内存 监控开发中...</p>
          </div>
        {/if}
      </div>
    </div>
  </div>
{/if}

<style>
  .modal-overlay {
    position: fixed;
    top: 0; left: 0; right: 0; bottom: 0;
    background: rgba(15, 23, 42, 0.6);
    backdrop-filter: blur(4px);
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 100;
  }

  .modal {
    background: white;
    width: 90%;
    max-width: 1000px;
    height: 85vh;
    border-radius: 16px;
    box-shadow: 0 25px 50px -12px rgba(0, 0, 0, 0.25);
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }

  .modal-header {
    padding: 1.25rem;
    border-bottom: 1px solid #e2e8f0;
    display: flex;
    justify-content: space-between;
    align-items: center;
  }

  .modal-header h3 { margin: 0; font-size: 1.1rem; }
  .close-btn { background: none; border: none; font-size: 1.5rem; cursor: pointer; color: #64748b; }

  .tabs {
    display: flex;
    border-bottom: 1px solid #e2e8f0;
    padding: 0 1.25rem;
    gap: 1.5rem;
  }

  .tabs button {
    background: none;
    border: none;
    padding: 1rem 0;
    font-size: 0.95rem;
    color: #64748b;
    cursor: pointer;
    border-bottom: 2px solid transparent;
  }

  .tabs button.active {
    color: #2563eb;
    border-bottom-color: #2563eb;
    font-weight: 500;
  }

  .modal-body {
    flex: 1;
    padding: 1.5rem;
    overflow-y: hidden;
    background: #f8fafc;
    display: flex;
    flex-direction: column;
  }

  /* Info Panel */
  .info-panel {
    display: flex;
    flex-direction: column;
    gap: 1.5rem;
  }

  .status-row {
    display: flex;
    align-items: center;
    gap: 1rem;
    font-size: 1rem;
  }

  .label { color: #64748b; width: 100px; }
  .value { font-weight: 500; }
  .status-healthy { color: #16a34a; }
  .status-degraded { color: #ca8a04; }
  .status-down { color: #dc2626; }

  .actions-group {
    background: white;
    padding: 1.5rem;
    border-radius: 12px;
    border: 1px solid #e2e8f0;
  }

  .actions-group h4 { margin: 0 0 1rem 0; font-size: 0.9rem; color: #64748b; text-transform: uppercase; }

  .buttons { display: flex; gap: 1rem; }

  .btn-action {
    padding: 0.5rem 1.5rem;
    border-radius: 8px;
    border: 1px solid #e2e8f0;
    background: white;
    cursor: pointer;
    font-weight: 500;
  }

  .btn-action.restart:hover { background: #eff6ff; border-color: #2563eb; color: #2563eb; }
  .btn-action.stop:hover { background: #fef2f2; border-color: #dc2626; color: #dc2626; }
  .btn-action:disabled { opacity: 0.5; cursor: not-allowed; }

  /* Logs Panel */
  .logs-panel {
    flex: 1;
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
    height: 100%;
    overflow: hidden;
  }

  .logs-controls {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 0.5rem;
    background: white;
    border: 1px solid #e2e8f0;
    border-radius: 8px;
    flex-shrink: 0;
  }

  .logs-viewer {
    flex: 1;
    background: #0f172a;
    color: #f1f5f9;
    border-radius: 8px;
    padding: 1rem;
    font-family: 'Menlo', 'Monaco', 'Courier New', monospace;
    font-size: 0.8rem;
    line-height: 1.5;
    overflow-y: auto;
    white-space: pre-wrap;
    word-break: break-all;
  }

  .log-line {
    display: flex;
    gap: 0.75rem;
    padding: 0.1rem 0;
    border-bottom: 1px solid rgba(255,255,255,0.05);
  }
  
  .log-line:hover {
    background: rgba(255,255,255,0.05);
  }

  .ts { color: #64748b; white-space: nowrap; font-size: 0.75rem; min-width: 70px; }
  .level { font-weight: bold; width: 45px; font-size: 0.75rem; text-align: center; border-radius: 2px; }
  .level.info { color: #60a5fa; background: rgba(59, 130, 246, 0.1); }
  .level.warn { color: #facc15; background: rgba(234, 179, 8, 0.1); }
  .level.error { color: #f87171; background: rgba(239, 68, 68, 0.1); }
  .msg { color: #e2e8f0; flex: 1; }

  .error-msg { color: #dc2626; margin-top: 0.5rem; font-size: 0.9rem; }
  .empty-logs { text-align: center; color: #64748b; padding: 2rem; font-style: italic; }
</style>