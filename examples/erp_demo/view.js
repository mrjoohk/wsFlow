export async function mount(container, api) {
  const heading = document.createElement('h3');
  heading.textContent = 'ERP 확장 모형 · 실제 연결 없음';
  const status = document.createElement('p');
  const details = document.createElement('pre');
  const refresh = document.createElement('button');
  refresh.textContent = '모형 자료 다시 읽기';
  container.append(heading, status, refresh, details);
  const load = async () => {
    refresh.disabled = true;
    try {
      const data = await api('GET', 'overview');
      status.textContent = data.company + ' · ' + data.message;
      details.textContent = JSON.stringify(data.items, null, 2);
    } catch (error) {
      status.textContent = error.message;
    } finally { refresh.disabled = false; }
  };
  refresh.onclick = load;
  await load();
}
