<script lang="ts">
  import { routeViewModels } from '$lib/viewModels/routes';

  const vm = routeViewModels.ticketDetail;
</script>

<section class="page-stack">
  <div class="section-heading">
    <p class="eyebrow">{vm.eyebrow}</p>
    <h1>{vm.title}</h1>
  </div>
  <div class="page-panel">
    <h2>{vm.panelTitle}</h2>
    <p>{vm.description}</p>
  </div>
</section>
