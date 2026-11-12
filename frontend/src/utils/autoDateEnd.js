import dayjs from 'dayjs'

export function handleRangeChange(obj, field) {
  const val = obj[field]
  if (val && val.length === 2) {
    const start = val[0]
    const end = dayjs(val[1]).endOf('day').format('YYYY-MM-DDTHH:mm:ss')
    obj[field] = [start, end]
  }
}
