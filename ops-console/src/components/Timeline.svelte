<script lang="ts">
  import { onMount } from "svelte";
  import { getServiceEvents, type ServiceEvent } from "../lib/api";

  export let range = "24h";
  let events: ServiceEvent[] = [];
  let loading = true;

  const eventColors = {
    start: "#22c55e",     // green
    error: "#ef4444",     // red
    restart: "#eab308",   // yellow
    self_heal: "#3b82f6", // blue
    crash: "#7f1d1d"      // dark red
  };

  const eventIcons = {
    start: "●",
    error: "×",
    restart: "↻",
    self_heal: "✚",
    crash: "☠"
  };

  async function loadEvents() {
    loading = true;
    try {
      events = await getServiceEvents(range);
    } finally {
      loading = false;
    }
  }

  onMount(() => {
    loadEvents();
  });

  function formatTime(ts: number) {
    return new Date(ts * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  }

  function getPosition(ts: number) {
    const now = Date.now() / 1000;
    const start = now - (range === "24h" ? 86400 : 604800);
    const total = now - start;
    const pos = ((ts - start) / total) * 100;
    return Math.max(0, Math.min(100, pos));
  }
  function getEventColor(type: string) {
    return eventColors[type as keyof typeof eventColors] || "#000";
  }
</script>

<div class="timeline-container">
  <div class="timeline-header">
    <h3>服务事件回顾 ({range})</h3>
    <div class="legend">
      {#each Object.entries(eventIcons) as [type, icon]}
        <div class="legend-item">
          <span class="icon" style="color: {getEventColor(type)}">{icon}</span>
          <span>{type}</span>
        </div>
      {/each}
    </div>
  </div>

  <div class="timeline-track">
    <div class="line"></div>
    {#each events as event}
      <div 
        class="event-marker" 
        style="left: {getPosition(event.timestamp)}%; background: {eventColors[event.type]}"
        title="{formatTime(event.timestamp)} - {event.service}: {event.details}"
      >
        <span class="marker-icon">{eventIcons[event.type]}</span>
      </div>
    {/each}
  </div>
</div>

<style>
  .timeline-container {
    background: white;
    padding: 1.5rem;
    border-radius: 16px;
    border: 1px solid #e2e8f0;
    margin-top: 1rem;
  }

  .timeline-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 2rem;
  }

  h3 { margin: 0; font-size: 1rem; color: #0f172a; }

  .legend { display: flex; gap: 1rem; }
  .legend-item { display: flex; gap: 0.25rem; align-items: center; font-size: 0.8rem; color: #64748b; }
  
  .timeline-track {
    position: relative;
    height: 40px;
    display: flex;
    align-items: center;
  }

  .line {
    position: absolute;
    width: 100%;
    height: 2px;
    background: #e2e8f0;
    top: 50%;
    transform: translateY(-50%);
  }

  .event-marker {
    position: absolute;
    width: 24px;
    height: 24px;
    border-radius: 50%;
    display: flex;
    align-items: center;
    justify-content: center;
    color: white;
    font-size: 12px;
    font-weight: bold;
    cursor: help;
    transform: translate(-50%, -50%);
    top: 50%;
    border: 2px solid white;
    box-shadow: 0 2px 4px rgba(0,0,0,0.1);
    transition: transform 0.2s;
    z-index: 1;
  }

  .event-marker:hover {
    transform: translate(-50%, -50%) scale(1.2);
    z-index: 10;
  }
</style>
