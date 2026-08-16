INSERT INTO learning_center_config (config_key, config_value, data_time)
VALUES
  (
    'base_url',
    'https://hcn3j6gp4127.feishu.cn/base/XAcGbyppoaxEDMs8q6dclHoYn0f',
    CURRENT_TIMESTAMP
  ),
  (
    'dashboard_url',
    'https://hcn3j6gp4127.feishu.cn/base/XAcGbyppoaxEDMs8q6dclHoYn0f?table=blkTg5qKiB8id2O6',
    CURRENT_TIMESTAMP
  ),
  (
    'template_url',
    'https://hcn3j6gp4127.feishu.cn/docx/OEkcdFpgmoU1rzxQK7actaawnbf',
    CURRENT_TIMESTAMP
  ),
  ('last_synced_at', '暂无数据', CURRENT_TIMESTAMP)
ON CONFLICT (config_key) DO UPDATE
SET
  config_value = EXCLUDED.config_value,
  data_time = EXCLUDED.data_time,
  _updated_at = CURRENT_TIMESTAMP;
