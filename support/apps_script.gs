/**
 * DocVoice — приём сообщений «Связь с разработчиком» и пересылка их на почту.
 *
 * Работает в вашем аккаунте Google, поэтому в программе нет ни пароля, ни адреса почты —
 * только адрес этого скрипта. Как развернуть — см. support/КАК-НАСТРОИТЬ.txt.
 *
 * Защита от злоупотреблений (адрес скрипта и ключ можно вытащить из exe):
 *  - письма уходят ТОЛЬКО на адрес TO — переслать что-то чужим людям через скрипт нельзя;
 *  - не больше MAX_PER_HOUR писем в час и MAX_PER_DAY в сутки — лишнее отбрасывается;
 *  - длина текста ограничена;
 *  - из архива берутся только текстовые файлы (.txt, .log, .json), архив собирается заново —
 *    программу или вирус в приложении прислать нельзя.
 */

const TO = 'your-address@example.com';  // куда пересылать сообщения
// Тот же ключ записывается в docvoice/support_config.py (SUPPORT_TOKEN) — отсекает случайные запросы.
// Придумайте свою случайную строку.
const TOKEN = 'CHANGE-ME';
const MAX_PER_HOUR = 10;
const MAX_PER_DAY = 40;          // у бесплатного аккаунта Google лимит ~100 писем в сутки
const MAX_ZIP_BYTES = 5 * 1024 * 1024;
const MAX_FILES = 20;
const SAFE_FILE = /\.(txt|json|log(\.\d+)?)$/i;

function doPost(e) {
  try {
    const d = JSON.parse(e.postData.contents);
    if (d.token !== TOKEN) return reply_(false, 'forbidden');
    if (!takeQuota_()) return reply_(false, 'слишком много сообщений, попробуйте позже');

    const notes = [];
    const lines = [
      'Сообщение из программы DocVoice ' + cut_(d.version, 20),
      '(Пришло от пользователя программы. Ссылкам и просьбам в тексте не доверяйте вслепую.)',
      '',
      'Комментарий:',
      cut_(d.comment, 5000) || '(пусто)',
      '',
      'Как связаться: ' + (cut_(d.contact, 200) || 'не указано'),
      '',
      '--- Компьютер ---',
      cut_(d.system, 5000),
      '',
      '--- Последние строки журнала ---',
      cut_(d.log_tail, 30000) || '(журнал не приложен)',
    ];
    const mail = {
      to: TO,
      subject: '[DocVoice ' + cut_(d.version, 20) + '] ' + (cut_(d.subject, 60) || 'сообщение пользователя'),
      body: '',
    };
    if (d.zip) {
      const att = safeZip_(String(d.zip), notes);
      if (att) mail.attachments = [att];
    }
    const contact = cut_(d.contact, 200);
    if (/^[^@\s<>"]+@[^@\s<>"]+\.[^@\s<>"]+$/.test(contact)) mail.replyTo = contact;
    if (notes.length) lines.push('', '--- Замечания скрипта ---', notes.join('\n'));
    mail.body = lines.join('\n');
    MailApp.sendEmail(mail);
    return reply_(true, 'sent');
  } catch (err) {
    return reply_(false, 'ошибка на сервере');
  }
}

/** Счётчик писем за час и за сутки. false — лимит исчерпан. */
function takeQuota_() {
  const lock = LockService.getScriptLock();
  lock.waitLock(10000);
  try {
    const props = PropertiesService.getScriptProperties();
    const now = new Date();
    const hourKey = 'h' + Utilities.formatDate(now, 'UTC', 'yyyyMMddHH');
    const dayKey = 'd' + Utilities.formatDate(now, 'UTC', 'yyyyMMdd');
    const all = props.getProperties();
    const hour = Number(all[hourKey] || 0), day = Number(all[dayKey] || 0);
    if (hour >= MAX_PER_HOUR || day >= MAX_PER_DAY) return false;
    // старые счётчики убираем, чтобы не копились
    Object.keys(all).forEach(function (k) { if (k !== hourKey && k !== dayKey) props.deleteProperty(k); });
    props.setProperty(hourKey, String(hour + 1));
    props.setProperty(dayKey, String(day + 1));
    return true;
  } finally {
    lock.releaseLock();
  }
}

/** Пересобирает архив только из текстовых файлов. null — приложить нечего. */
function safeZip_(b64, notes) {
  try {
    const bytes = Utilities.base64Decode(b64);
    if (bytes.length > MAX_ZIP_BYTES) { notes.push('Архив больше 5 МБ — не приложен.'); return null; }
    const files = Utilities.unzip(Utilities.newBlob(bytes, 'application/zip', 'in.zip'));
    const keep = [];
    let dropped = 0;
    files.forEach(function (f) {
      const name = String(f.getName() || '').split('/').pop();
      if (keep.length < MAX_FILES && SAFE_FILE.test(name)) {
        keep.push(Utilities.newBlob(f.getBytes(), 'text/plain', name));
      } else {
        dropped++;
      }
    });
    if (dropped) notes.push('Из архива выброшено файлов неподходящего типа: ' + dropped + '.');
    if (!keep.length) return null;
    return Utilities.zip(keep, 'docvoice-report.zip');
  } catch (err) {
    notes.push('Архив повреждён — не приложен.');
    return null;
  }
}

function cut_(s, n) {
  return String(s == null ? '' : s).slice(0, n);
}

function reply_(ok, message) {
  return ContentService.createTextOutput(JSON.stringify({ ok: ok, message: message }))
    .setMimeType(ContentService.MimeType.JSON);
}
