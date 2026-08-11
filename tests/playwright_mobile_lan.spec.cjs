const { test, expect } = require('playwright/test');

test('пользователь видит причину недоступности и может выбрать Wi-Fi вместо VPN', async ({ page }) => {
  let firewallReady = false;
  const json = (body, status = 200) => ({
    status,
    contentType: 'application/json; charset=utf-8',
    body: JSON.stringify(body),
  });

  await page.route('**/api/**', async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path === '/api/identity') return route.fulfill(json({ assistant_name: 'Эрви', user_address: 'Пользователь', onboarding_completed: true }));
    if (path === '/api/style') return route.fulfill(json({ answer_length: 'средняя', directness: 4, humor: '' }));
    if (path === '/api/preferences') return route.fulfill(json({ version: '1.7.3', build: 'r37-mobile-clean', sphere_motion: true }));
    if (path === '/api/tasks') return route.fulfill(json([]));
    if (path === '/api/voice/runtime') return route.fulfill(json({ running: true, state: 'idle', wake_phrase: 'Эрви', onboarding_complete: true }));
    if (path === '/api/runtime') return route.fulfill(json({ cancellable: false }));
    if (path === '/api/conversations' && request.method() === 'POST') return route.fulfill(json({ id: 'pw-conversation' }));
    if (path === '/api/conversations/pw-conversation') return route.fulfill(json({ id: 'pw-conversation', messages: [] }));
    if (path === '/api/mobile/config') return route.fulfill(json({
      preferred_address: 'http://192.168.1.34:7860',
      addresses: ['http://192.168.1.34:7860', 'http://10.8.0.2:7860'],
      address_options: [
        { url: 'http://192.168.1.34:7860', ip: '192.168.1.34', interface: 'Wi-Fi', kind: 'wifi', recommended: true, warning: '' },
        { url: 'http://10.8.0.2:7860', ip: '10.8.0.2', interface: 'My VPN', kind: 'virtual', recommended: false, warning: 'Это виртуальный/VPN-адаптер; телефон обычно не видит его.' },
      ],
      token: 'ABCDE-FGHIJ-KLMNO-PQRST',
      lan_enabled: true,
      firewall_ready: firewallReady,
      detail: firewallReady
        ? 'Windows Firewall разрешает EIRVEN на порту 7860 только устройствам этого локального сегмента.'
        : 'Windows Firewall пока блокирует телефон. Перезапусти EIRVEN через ярлык и подтверди системный запрос UAC.',
      apk_available: true,
      download_url: 'http://192.168.1.34:7860/api/mobile/app.apk',
      install_url: 'http://192.168.1.34:7860/mobile/install',
    }));
    return route.fulfill(json({}));
  });

  await page.goto('/ui/');
  await page.getByRole('button', { name: 'Настройки' }).click();
  await page.getByRole('button', { name: 'Телефон', exact: true }).click();

  await expect(page.locator('#mobile-address')).toHaveText('http://192.168.1.34:7860');
  await expect(page.locator('#mobile-download-qr svg')).toBeVisible();
  await expect(page.locator('#mobile-network-detail')).toHaveClass(/error/);
  await expect(page.locator('#mobile-network-detail')).toContainText('Windows Firewall пока блокирует телефон');
  await expect(page.locator('#mobile-address-options')).toBeVisible();

  await page.getByRole('button', { name: /VPN\/виртуальная/ }).click();
  await expect(page.locator('#mobile-address')).toHaveText('http://10.8.0.2:7860');
  await expect(page.locator('#mobile-network-detail')).toContainText('телефон обычно его не видит');
  await expect(page.locator('#mobile-download-link')).toHaveAttribute('href', 'http://10.8.0.2:7860/mobile/install');

  firewallReady = true;
  await page.locator('#refresh-mobile-network').click();
  await expect(page.locator('#mobile-address')).toHaveText('http://192.168.1.34:7860');
  await expect(page.locator('#mobile-network-detail')).toHaveClass(/ready/);
  await expect(page.locator('#mobile-network-detail')).toContainText('Выбран интерфейс «Wi-Fi»');
  await expect(page.locator('#mobile-status')).toContainText('Готово');

  await page.screenshot({ path: '/tmp/eirven-r37-phone-panel.png', fullPage: true });
});

test('телефон видит подтверждение связи и скачивает APK с понятным именем', async ({ page }) => {
  const installUrl = process.env.EIRVEN_MOBILE_INSTALL_URL;
  test.skip(!installUrl, 'Запускается только вместе с локальным API EIRVEN');
  await page.goto(installUrl);
  await expect(page.getByRole('heading', { name: 'Связь с компьютером есть' })).toBeVisible();
  await expect(page.getByRole('link', { name: 'Скачать EIRVEN Mobile 1.9.6' })).toBeVisible();
  const downloadPromise = page.waitForEvent('download');
  await page.getByRole('link', { name: 'Скачать EIRVEN Mobile 1.9.6' }).click();
  const download = await downloadPromise;
  expect(download.suggestedFilename()).toBe('EIRVEN-Mobile-1.9.6.apk');
  await page.screenshot({ path: '/tmp/eirven-r37-phone-install.png', fullPage: true });
});

test('стиль общения выбирается кнопками и заметно переключается', async ({ page }) => {
  const json = (body, status = 200) => ({ status, contentType: 'application/json; charset=utf-8', body: JSON.stringify(body) });
  let savedHumor = 'сдержанный живой тон, почти без шуток';
  await page.route('**/api/**', async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path === '/api/identity') return route.fulfill(json({ assistant_name: 'Эрви', user_address: 'Пользователь', onboarding_completed: true }));
    if (path === '/api/style') {
      if (request.method() === 'PUT') savedHumor = JSON.parse(request.postData() || '{}').humor || savedHumor;
      return route.fulfill(json({ answer_length: 'средняя', directness: 4, humor: savedHumor }));
    }
    if (path === '/api/preferences') return route.fulfill(json({ version: '1.7.3', build: 'r37-mobile-clean', sphere_motion: true }));
    if (path === '/api/tasks') return route.fulfill(json([]));
    if (path === '/api/voice/runtime') return route.fulfill(json({ running: true, state: 'idle', wake_phrase: 'Эрви', onboarding_complete: true }));
    if (path === '/api/runtime') return route.fulfill(json({ cancellable: false }));
    if (path === '/api/conversations' && request.method() === 'POST') return route.fulfill(json({ id: 'pw-style' }));
    if (path === '/api/conversations/pw-style') return route.fulfill(json({ id: 'pw-style', messages: [] }));
    return route.fulfill(json({}));
  });

  await page.goto('/ui/');
  await page.getByRole('button', { name: 'Настройки' }).click();
  await page.getByRole('button', { name: 'Голос', exact: true }).click();
  await expect(page.locator('#communication-style .preset-chip')).toHaveCount(4);
  await page.getByRole('button', { name: /Свободно и с юмором/ }).click();
  await expect(page.locator('#communication-style .preset-chip.active')).toContainText('Свободно и с юмором');
  await page.getByRole('button', { name: /Коротко и по делу/ }).click();
  await expect(page.locator('#communication-style .preset-chip.active')).toContainText('Коротко и по делу');
});
