const modal = document.querySelector('#modal-demo')
document.querySelector('#open-modal')?.addEventListener('click', () => { modal.hidden = false })
modal?.querySelectorAll('[data-close]').forEach(button => button.addEventListener('click', () => { modal.hidden = true }))
modal?.addEventListener('click', event => { if (event.target === modal) modal.hidden = true })
document.querySelector('.switch input')?.addEventListener('change', event => {
  document.querySelector('#switch-value').textContent = event.target.checked ? '已启用' : '已关闭'
})
const scrollDemo = document.querySelector('.scroll-demo')
if (scrollDemo) scrollDemo.innerHTML = Array.from({ length: 12 }, (_, index) => `<div><b>${String(index + 1).padStart(2, '0')}</b><span>NOOR SDK scroll item</span></div>`).join('')
