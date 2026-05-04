<section class="pma-layout">
  <div class="chat-list">
    <div class="section-heading">
      <p class="eyebrow">PMA rooms</p>
      <h1>Active conversations</h1>
    </div>
    <div class="filter-row">
      <button class="chip active" type="button">All</button>
      <button class="chip" type="button">Active</button>
      <button class="chip" type="button">Waiting</button>
    </div>
    <article class="chat-card active">
      <div>
        <strong>Hub rewrite foundation</strong>
        <p>PMA is setting up the new app shell.</p>
      </div>
      <span class="status-pill running">Running</span>
    </article>
    <article class="chat-card">
      <div>
        <strong>Local workspace</strong>
        <p>No pending approvals.</p>
      </div>
      <span class="status-pill idle">Idle</span>
    </article>
  </div>

  <div class="active-chat">
    <div class="chat-header">
      <div>
        <p class="eyebrow">Primary room</p>
        <h2>PMA</h2>
      </div>
      <div class="selector-row">
        <select aria-label="PMA agent">
          <option>Codex PMA</option>
        </select>
        <select aria-label="Model">
          <option>Configured model</option>
        </select>
      </div>
    </div>

    <div class="message-stack">
      <article class="message user">
        <p>Build the new PMA Hub foundation.</p>
      </article>
      <article class="message assistant">
        <p>The new shell is ready for PMA-first workflows, with repos, tickets, dashboard, and settings as supporting routes.</p>
      </article>
      <article class="artifact-card">
        <span class="artifact-type">Progress</span>
        <strong>Frontend foundation</strong>
        <p>Parallel SvelteKit app, design tokens, route-aware navigation, and static serving are wired.</p>
      </article>
    </div>

    <form class="composer" onsubmit={(event) => event.preventDefault()}>
      <button class="icon-button" type="button" aria-label="Attach files" title="Attach files">+</button>
      <textarea aria-label="Message PMA" placeholder="Message PMA about this workspace"></textarea>
      <button class="send-button" type="submit">Send</button>
    </form>
  </div>
</section>
