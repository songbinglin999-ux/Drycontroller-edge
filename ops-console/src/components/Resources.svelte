<script lang="ts">
  import { type SystemMetricsPayload } from "../lib/api";

  export let metrics: SystemMetricsPayload | null = null;

  $: cpuPercent = metrics?.cpu?.percent ?? 0;
  $: memPercent = metrics?.memory?.percent ?? 0;
  $: diskPercent = metrics?.disk?.percent ?? 0;
  
  $: memUsed = metrics?.memory?.used ? (metrics.memory.used / 1024 / 1024 / 1024).toFixed(1) : "0";
  $: memTotal = metrics?.memory?.total ? (metrics.memory.total / 1024 / 1024 / 1024).toFixed(1) : "0";
  $: diskUsed = metrics?.disk?.used ? (metrics.disk.used / 1024 / 1024 / 1024).toFixed(1) : "0";
  $: diskTotal = metrics?.disk?.total ? (metrics.disk.total / 1024 / 1024 / 1024).toFixed(1) : "0";

  $: load1 = metrics?.cpu?.load_avg?.["1m"]?.toFixed(2) ?? "—";
  $: load5 = metrics?.cpu?.load_avg?.["5m"]?.toFixed(2) ?? "—";
  $: load15 = metrics?.cpu?.load_avg?.["15m"]?.toFixed(2) ?? "—";

  function getProgressColor(percent: number) {
    if (percent >= 90) return "#ef4444";
    if (percent >= 75) return "#f59e0b";
    return "#3b82f6";
  }
</script>

<div class="resources-grid">
  <!-- CPU Panel -->
  <div class="resource-card">
    <div class="card-header">
      <div class="title-group">
        <span class="icon cpu">
          <svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="4" y="4" width="16" height="16" rx="2" ry="2"></rect><rect x="9" y="9" width="6" height="6"></rect><line x1="9" y1="1" x2="9" y2="4"></line><line x1="15" y1="1" x2="15" y2="4"></line><line x1="9" y1="20" x2="9" y2="23"></line><line x1="15" y1="20" x2="15" y2="23"></line><line x1="20" y1="9" x2="23" y2="9"></line><line x1="20" y1="14" x2="23" y2="14"></line><line x1="1" y1="9" x2="4" y2="9"></line><line x1="1" y1="14" x2="4" y2="14"></line></svg>
        </span>
        <h3>CPU</h3>
      </div>
      <span class="value">{cpuPercent}%</span>
    </div>
    
    <div class="progress-bg">
      <div 
        class="progress-bar" 
        style="width: {cpuPercent}%; background: {getProgressColor(cpuPercent)}"
      ></div>
    </div>

    <div class="details">
      <div class="detail-item">
        <span class="label">Load (1m)</span>
        <span class="val">{load1}</span>
      </div>
      <div class="detail-item">
        <span class="label">Load (5m)</span>
        <span class="val">{load5}</span>
      </div>
      <div class="detail-item">
        <span class="label">Load (15m)</span>
        <span class="val">{load15}</span>
      </div>
    </div>
  </div>

  <!-- Memory Panel -->
  <div class="resource-card">
    <div class="card-header">
      <div class="title-group">
        <span class="icon mem">
          <svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"></path><polyline points="3.27 6.96 12 12.01 20.73 6.96"></polyline><line x1="12" y1="22.08" x2="12" y2="12"></line></svg>
        </span>
        <h3>内存</h3>
      </div>
      <span class="value">{memPercent}%</span>
    </div>

    <div class="progress-bg">
      <div 
        class="progress-bar" 
        style="width: {memPercent}%; background: {getProgressColor(memPercent)}"
      ></div>
    </div>

    <div class="details">
      <div class="detail-item">
        <span class="label">已用</span>
        <span class="val">{memUsed} GB</span>
      </div>
      <div class="detail-item">
        <span class="label">总计</span>
        <span class="val">{memTotal} GB</span>
      </div>
    </div>
  </div>

  <!-- Disk Panel -->
  <div class="resource-card">
    <div class="card-header">
      <div class="title-group">
        <span class="icon disk">
          <svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><ellipse cx="12" cy="5" rx="9" ry="3"></ellipse><path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3"></path><path d="M3 5v14c0 1.66 4 3 9 3s 9-1.34 9-3V5"></path></svg>
        </span>
        <h3>磁盘</h3>
      </div>
      <span class="value">{diskPercent}%</span>
    </div>

    <div class="progress-bg">
      <div 
        class="progress-bar" 
        style="width: {diskPercent}%; background: {getProgressColor(diskPercent)}"
      ></div>
    </div>

    <div class="details">
      <div class="detail-item">
        <span class="label">已用</span>
        <span class="val">{diskUsed} GB</span>
      </div>
      <div class="detail-item">
        <span class="label">总计</span>
        <span class="val">{diskTotal} GB</span>
      </div>
    </div>
  </div>
</div>

<style>
  .resources-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
    gap: 1rem;
    margin-bottom: 2rem;
  }

  .resource-card {
    background: white;
    border: 1px solid #e2e8f0;
    border-radius: 12px;
    padding: 1.25rem;
    box-shadow: 0 1px 3px rgba(0,0,0,0.05);
  }

  .card-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 1rem;
  }

  .title-group {
    display: flex;
    align-items: center;
    gap: 0.75rem;
  }

  .icon {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 36px;
    height: 36px;
    border-radius: 8px;
    color: white;
  }

  .icon.cpu { background: #8b5cf6; }
  .icon.mem { background: #10b981; }
  .icon.disk { background: #f59e0b; }

  h3 {
    margin: 0;
    font-size: 1rem;
    color: #0f172a;
    font-weight: 600;
  }

  .value {
    font-size: 1.25rem;
    font-weight: 700;
    color: #0f172a;
  }

  .progress-bg {
    height: 8px;
    background: #f1f5f9;
    border-radius: 999px;
    overflow: hidden;
    margin-bottom: 1rem;
  }

  .progress-bar {
    height: 100%;
    transition: width 0.5s ease, background-color 0.3s ease;
  }

  .details {
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 0.5rem;
    padding-top: 0.75rem;
    border-top: 1px solid #f1f5f9;
  }

  .detail-item {
    display: flex;
    flex-direction: column;
    align-items: center;
    text-align: center;
  }

  .label {
    font-size: 0.75rem;
    color: #64748b;
    margin-bottom: 0.25rem;
  }

  .val {
    font-size: 0.9rem;
    font-weight: 500;
    color: #334155;
  }
</style>
