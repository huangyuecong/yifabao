<script lang="tsx">
import RouterSearch from '@/components/RouterSearch/index.vue'
import { useDesign } from '@/hooks/web/useDesign'
import { Message } from '@/layout/components//Message'
import { Collapse } from '@/layout/components/Collapse'
import { Screenfull } from '@/layout/components/Screenfull'
import { UserInfo } from '@/layout/components/UserInfo'
import { useAppStore } from '@/store/modules/app'
import { computed, defineComponent } from '@/vue-export'

const { getPrefixCls, variables } = useDesign()

const prefixCls = getPrefixCls('tool-header')

const appStore = useAppStore()

// 面包屑
// const breadcrumb = computed(() => appStore.getBreadcrumb)

// 折叠图标
const hamburger = computed(() => appStore.getHamburger)

// 全屏图标
const screenfull = computed(() => appStore.getScreenfull)

// 搜索图片
const search = computed(() => appStore.search)

// 尺寸图标
const size = computed(() => appStore.getSize)

const layout = computed(() => appStore.getLayout)

const title = computed(() => appStore.getTitle)

// 多语言图标
const locale = computed(() => appStore.getLocale)

// 消息图标
const message = computed(() => appStore.getMessage)

const toolCircleClass = 'tool-header__circle ml-8px h-32px w-32px flex flex-[0_0_32px] items-center justify-center rounded-[50%] border border-solid border-[#eee] cursor-pointer'
const toolSearchClass = 'tool-header__search ml-8px h-32px flex flex-none items-center rounded-[16px] border border-solid border-[#eee] cursor-pointer'

export default defineComponent({
  name: 'ToolHeader',
  props: {
    hideNavigation: { type: Boolean, default: false },
  },
  setup(props) {
    return () => (
      <div
        id={`${variables.namespace}-tool-header`}
        class={[
          prefixCls,
          'h-[var(--top-tool-height)] relative px-[var(--top-tool-p-x)] flex items-center justify-between',
          'dark:bg-[var(--el-bg-color)]',
        ]}
      >
        {!props.hideNavigation && layout.value !== 'top'
          ? (
              <div class="h-full flex flex-[0_0_30px] items-center">
                {hamburger.value && layout.value !== 'cutMenu'
                  ? (
                      <Collapse class="custom-hover" color="var(--top-header-text-color)"></Collapse>
                    )
                  : undefined}
                {/* {breadcrumb.value ? <Breadcrumb class="lt-md:hidden"></Breadcrumb> : undefined} */}
              </div>
            )
          : undefined}
        <div class="tool-header__title h-full flex flex-1 items-center text-20px text-[var(--el-color-primary)] font-600">
          {title.value}

        </div>
        <div class="h-full flex items-center">
          {search.value
            ? (
                <div class={toolSearchClass}>
                  <RouterSearch isModal={false} />
                </div>
              )
            : undefined}
          {screenfull.value
            ? (
                <Screenfull class={toolCircleClass} color="var(--top-header-text-color)"></Screenfull>
              )
            : undefined}
          {/* {size.value
            ? (
                <SizeDropdown class="custom-hover" color="var(--top-header-text-color)"></SizeDropdown>
              )
            : undefined}
          {locale.value
            ? (
                <LocaleDropdown
                  class="custom-hover"
                  color="var(--top-header-text-color)"
                >
                </LocaleDropdown>
              )
            : undefined} */}
          {message.value
            ? (
                <Message class={toolCircleClass} color="var(--top-header-text-color)"></Message>
              )
            : undefined}
          <UserInfo></UserInfo>
        </div>
      </div>
    )
  },
})
</script>

<style lang="scss" scoped>
$prefix-cls: #{$namespace}-tool-header;

.#{$prefix-cls} {
  background: url('@/assets/imgs/top_bg.png') no-repeat right bottom #fff;
  transition: left var(--transition-time-02);

  .tool-header__title {
    min-width: 0;
    line-height: 24px;
    overflow-wrap: anywhere;
  }

  @media (max-width: 767px) {
    .tool-header__title {
      font-size: 16px;
      line-height: 22px;
    }
  }

  .tool-header__circle {
    box-sizing: border-box;
    transition: background var(--transition-time-02);
    background: #fff;

    &:hover {
      background-color: var(--top-header-hover-color);
    }
  }

  :deep(.tool-header__circle .custom-hover) {
    width: 100%;
    height: 100%;
    padding-right: 0;
    justify-content: center;
  }

  .tool-header__search {
    box-sizing: border-box;
    min-width: 32px;
    transition:
      background var(--transition-time-02),
      border-radius var(--transition-time-02);
    background: #fff;

    &:hover {
      background-color: var(--top-header-hover-color);
    }

    &:has(.custom-hover.is-active) {
      border-radius: 0;
      background-color: #fff;
    }
  }

  :deep(.tool-header__search .custom-hover) {
    display: flex;
    align-items: center;
    width: 100%;
    height: 100%;
    padding: 0 8px;
  }

  &::after {
    content: '';
    position: fixed;
    top: 0;
    left: 0;
    width: 100%;
    height: 64px;
    background: transparent;
    pointer-events: none;
    box-shadow:
      0px 8px 20px 0px rgba(0, 0, 0, 0.04),
      0px 12px 32px 4px rgba(0, 0, 0, 0.02);
    z-index: -1;
  }
}
</style>
