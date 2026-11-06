<template>
	<ion-page>
		<ion-content class="ion-padding">
			<div class="flex h-screen w-screen flex-col justify-center bg-white">
				<Dialog
					:options="{
						title: __('Login Failed'),
						message: __('No active employee found associated with the email ID {0}. Try logging in with your employee email ID or contact your HR manager for access.', [session?.user]),
						size: 'lg',
						actions: [
							{
								label: __('Go to Login'),
								variant: 'solid',
								onClick: () => session.logout.submit(),
							},
						],
					}"
					v-model="showDialog"
					@close="closeDialog"
				/>
			</div>
		</ion-content>
	</ion-page>
</template>

<script setup>
import { IonPage, IonContent } from "@ionic/vue"
import { inject, ref } from "vue"
import { Dialog } from "frappe-ui"
import { mpassPortalUrl } from "@/data/session"

const session = inject("$session")
const showDialog = ref(true)

// Under SSO the user stays signed in and goes back to the landing portal.
function closeDialog() {
	const portal = mpassPortalUrl()
	if (portal) {
		window.location.href = portal
		return
	}
	session.logout.submit()
	showDialog.value = false
}
</script>
