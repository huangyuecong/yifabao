import { ref } from 'vue'

/** Pending-review count shared by the sidebar badge and the review page, so both change together. */
export const pendingReviewCount = ref(0)
