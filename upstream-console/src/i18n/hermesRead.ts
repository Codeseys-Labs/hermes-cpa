// Isolated console i18n: deliberately no storage, language-store or network imports.
import i18n from 'i18next';
import { initReactI18next } from 'react-i18next';
import en from './hermes-read.en.json';
import upstreamEn from './locales/en.json';
import upstreamCN from './locales/zh-CN.json';
import upstreamTW from './locales/zh-TW.json';
import upstreamRu from './locales/ru.json';
import upstreamKo from './locales/ko.json';
import upstreamVi from './locales/vi.json';

export const resources = {
  en: { translation: en },
  'zh-CN': { translation: { ...en, providers: '提供商', auth: '认证文件 / OAuth',
    models: '模型 / 别名', quota: '配额', logs: '日志', plugins: '插件', status: '状态',
    usage: '用量', config: '配置', refresh: '刷新安全视图', loading: '加载中…',
    entry: '条目', provider: '提供商', priority: '配置优先级', credentials: '配置凭据数',
    disabled_credentials: '明确禁用的凭据数', model: '固定目录模型标识',
    context_length: '上下文令牌数', max_completion_tokens: '最大输出令牌数', severity: '严重程度',
    unknown: '未知', entries: '{{count}} 条', withheld: '已隐藏 {{count}} 个未知模型标识',
    mutations: '凭据 / OAuth 更改', clear: '清除日志', quota_refresh: '刷新提供商配额', aliases: '编辑别名',
    unavailable: '不可用，不代表零。请检查管理服务和固定的 CPA 版本。',
    providers_note: '显示持久化 API 密钥组配置，不代表凭据实时健康状态。隐藏名称、网址和密钥。忽略未知提供商。缺失优先级表示未知。',
    auth_note: '不支持认证文件和 OAuth 状态：CPA 8.0.21 列举凭据会调用配额或认证插件，并可能读取凭据文件，无法保证只读。不会登录、刷新令牌或获取凭据。',
    models_note: '仅显示 Codex 静态目录，不代表账户可用性。标识必须匹配固定目录，其他标识均隐藏。别名可能含密钥，因此不支持。不会调用推理接口。',
    quota_note: '不支持配额观察：列举凭据也可能执行插件回调。远程配额读取或刷新可能调用提供商。没有安全的被动快照接口，不推测百分比、不调用提供商。',
    logs_note: '仅显示最近 100 行日志的严重程度。未知前缀标记为未知。隐藏消息、路径、请求标识、游标并禁用下载。文件日志必须已启用。',
    plugins_note: '不支持插件清单：枚举注册插件可能调用插件标识回调。禁用自定义扩展执行和安装。',
    disabled: '禁用凭据、认证文件、OAuth、别名编辑、配额刷新和日志清除。CPA 不提供这些写入的原子版本前置条件，失败可能留下部分更改。原生确认不能保证原子性。完整功能对等已推迟。' } },
  'zh-TW': { translation: { ...en, providers: '提供者', auth: '驗證檔案 / OAuth',
    models: '模型 / 別名', quota: '配額', logs: '日誌', plugins: '外掛', status: '狀態',
    usage: '用量', config: '設定', refresh: '重新整理安全檢視', loading: '載入中…',
    entry: '項目', provider: '提供者', priority: '設定優先順序', credentials: '設定憑證數',
    disabled_credentials: '明確停用的憑證數', model: '固定目錄模型識別碼',
    context_length: '上下文權杖數', max_completion_tokens: '最大輸出權杖數', severity: '嚴重程度',
    unknown: '未知', entries: '{{count}} 筆', withheld: '已隱藏 {{count}} 個未知模型識別碼',
    mutations: '憑證 / OAuth 變更', clear: '清除日誌', quota_refresh: '重新整理提供者配額', aliases: '編輯別名',
    unavailable: '無法使用，不代表零。請檢查管理服務與固定 CPA 版本。',
    providers_note: '顯示持久化 API 金鑰群組設定，不代表憑證即時健康狀態。隱藏名稱、網址和金鑰。忽略未知提供者。缺失優先順序表示未知。',
    auth_note: '不支援驗證檔案和 OAuth 狀態：CPA 8.0.21 列舉憑證會呼叫配額或驗證外掛，並可能讀取憑證檔案，無法保證唯讀。不會登入、更新權杖或取得憑證。',
    models_note: '僅顯示 Codex 靜態目錄，不代表帳戶可用性。識別碼必須符合固定目錄，其他識別碼均隱藏。別名可能含金鑰，因此不支援。不會呼叫推理介面。',
    quota_note: '不支援配額觀察：列舉憑證也可能執行外掛回呼。遠端配額讀取或重新整理可能呼叫提供者。沒有安全的被動快照介面，不推測百分比、不呼叫提供者。',
    logs_note: '僅顯示最近 100 行日誌的嚴重程度。未知前綴標記為未知。隱藏訊息、路徑、請求識別碼、游標並停用下載。檔案日誌必須已啟用。',
    plugins_note: '不支援外掛清單：列舉註冊外掛可能呼叫識別回呼。停用自訂擴充執行和安裝。',
    disabled: '停用憑證、驗證檔案、OAuth、別名編輯、配額重新整理和日誌清除。CPA 不提供這些寫入的原子版本前置條件，失敗可能留下部分變更。原生確認不能保證原子性。完整功能對等已延後。' } },
  ru: { translation: { ...en, providers: 'Провайдеры', auth: 'Файлы авторизации / OAuth',
    models: 'Модели / псевдонимы', quota: 'Квота', logs: 'Журналы', plugins: 'Плагины',
    status: 'Состояние', usage: 'Использование', config: 'Конфигурация', refresh: 'Обновить безопасное представление',
    loading: 'Загрузка…', entry: 'Запись', provider: 'Провайдер', priority: 'Приоритет',
    credentials: 'Настроенные учётные данные', disabled_credentials: 'Явно отключённые',
    model: 'Идентификатор из закреплённого каталога', context_length: 'Токены контекста',
    max_completion_tokens: 'Максимум выходных токенов', severity: 'Уровень', unknown: 'Неизвестно',
    entries: 'Записей: {{count}}', withheld: 'Скрыто неизвестных идентификаторов: {{count}}',
    mutations: 'Изменить учётные данные / OAuth', clear: 'Очистить журналы', quota_refresh: 'Обновить квоту провайдера', aliases: 'Изменить псевдонимы',
    unavailable: 'Недоступно — не ноль. Проверьте готовность API управления и версию CPA.',
    providers_note: 'Сохранённая конфигурация групп API-ключей, не состояние авторизации. Имена, адреса и ключи скрыты. Неизвестные провайдеры пропущены. Отсутствующий приоритет не означает ноль.',
    auth_note: 'Состояние файлов авторизации и OAuth не поддерживается: список учётных данных CPA 8.0.21 вызывает плагины и может читать файлы авторизации. Безопасный пассивный контракт отсутствует. Вход и обновление токенов не выполняются.',
    models_note: 'Только статический каталог Codex, не доступность для аккаунта. Разрешены лишь идентификаторы закреплённого каталога. Псевдонимы скрыты: произвольные имена могут содержать секреты. Запросов инференса нет.',
    quota_note: 'Наблюдение квоты не поддерживается: даже перечисление учётных данных вызывает плагины. Чтение и обновление квоты могут обращаться к провайдерам. Безопасного пассивного снимка нет; проценты не вычисляются, провайдеры не вызываются.',
    logs_note: 'Только уровни последних 100 строк. Неизвестный префикс отмечается как неизвестный. Сообщения, пути, идентификаторы и курсоры скрыты; загрузка отключена. Запись журналов в файл должна быть уже включена.',
    plugins_note: 'Список плагинов не поддерживается: перечисление может вызывать код плагинов. Выполнение расширений и установка отключены.',
    disabled: 'Запись учётных данных, файлов авторизации, OAuth, псевдонимов, обновление квот и очистка журналов отключены. CPA не обеспечивает атомарную проверку ревизии; ошибка может оставить частичные изменения. Подтверждение не заменяет атомарность. Полный паритет отложен.' } },
};
const shellResources = {
  en: { translation: { ...resources.en.translation, ...upstreamEn, hermes_filter: 'Filter released fields' } },
  'zh-CN': { translation: { ...resources['zh-CN'].translation, ...upstreamCN, hermes_filter: '筛选已公开字段' } },
  'zh-TW': { translation: { ...resources['zh-TW'].translation, ...upstreamTW, hermes_filter: '篩選已公開欄位' } },
  ru: { translation: { ...resources.ru.translation, ...upstreamRu, hermes_filter: 'Фильтр доступных полей' } },
  ko: { translation: { ...en, ...upstreamKo, hermes_filter: '공개 필드 필터' } },
  vi: { translation: { ...en, ...upstreamVi, hermes_filter: 'Lọc trường được công bố' } },
};
i18n.use(initReactI18next).init({ resources: shellResources, lng: 'en', fallbackLng: 'en',
  interpolation: { escapeValue: false }, react: { useSuspense: false } });
export default i18n;
