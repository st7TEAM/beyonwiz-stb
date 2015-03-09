from boxbranding import getMachineBrand, getMachineName
import xml.etree.cElementTree
from time import localtime, strftime, ctime, time
from bisect import insort
from sys import maxint
import os

from enigma import eEPGCache, getBestPlayableServiceReference, eServiceReference, eServiceCenter, iRecordableService, quitMainloop, eActionMap, setPreferredTuner

from Components.config import config
from Components import Harddisk
from Components.UsageConfig import defaultMoviePath
from Components.TimerSanityCheck import TimerSanityCheck
from Screens.MessageBox import MessageBox
import Screens.Standby
import Screens.InfoBar
from Tools import Directories, Notifications, ASCIItranslit, Trashcan
from Tools.XMLTools import stringToXML
import timer
import NavigationInstance
from ServiceReference import ServiceReference


# In descriptions etc. we have:
# service reference	(to get the service name)
# name			(title)
# description		(description)
# event data		(ONLY for time adjustments etc.)


# Parses an event, and returns a (begin, end, name, duration, eit)-tuple.
# begin and end will be adjusted by the margin before/after recording start/end
def parseEvent(ev, description=True):
	if description:
		name = ev.getEventName()
		description = ev.getShortDescription()
		if description == "":
			description = ev.getExtendedDescription()
	else:
		name = ""
		description = ""
	begin = ev.getBeginTime()
	end = begin + ev.getDuration()
	eit = ev.getEventId()
	begin -= config.recording.margin_before.value * 60
	end += config.recording.margin_after.value * 60
	return begin, end, name, description, eit

class AFTEREVENT:
	def __init__(self):
		pass

	NONE = 0
	STANDBY = 1
	DEEPSTANDBY = 2
	AUTO = 3

def findSafeRecordPath(dirname):
	if not dirname:
		return None
	dirname = os.path.realpath(dirname)
	mountpoint = Harddisk.findMountPoint(dirname)
	if not os.path.ismount(mountpoint):
		print '[RecordTimer] media is not mounted:', dirname
		return None
	if not os.path.isdir(dirname):
		try:
			os.makedirs(dirname)
		except Exception, ex:
			print '[RecordTimer] Failed to create dir "%s":' % dirname, ex
			return None
	return dirname

# type 1 = digital television service
# type 4 = nvod reference service (NYI)
# type 17 = MPEG-2 HD digital television service
# type 22 = advanced codec SD digital television
# type 24 = advanced codec SD NVOD reference service (NYI)
# type 25 = advanced codec HD digital television
# type 27 = advanced codec HD NVOD reference service (NYI)
# type 2 = digital radio sound service
# type 10 = advanced codec digital radio sound service

service_types_tv = '1:7:1:0:0:0:0:0:0:0:(type == 1) || (type == 17) || (type == 22) || (type == 25) || (type == 134) || (type == 195)'
wasRecTimerWakeup = False

# Please do not translate log messages
class RecordTimerEntry(timer.TimerEntry, object):
	def __init__(self, serviceref, begin, end, name, description, eit, disabled=False, justplay=False, afterEvent=AFTEREVENT.AUTO, checkOldTimers=False, dirname=None, tags=None, descramble='notset', record_ecm='notset', isAutoTimer=False, ice_timer_id=None, always_zap=False, rename_repeat=True):
		timer.TimerEntry.__init__(self, int(begin), int(end))
		if checkOldTimers:
			if self.begin < time() - 1209600:  # 2 weeks
				self.begin = int(time())

		if self.end < self.begin:
			self.end = self.begin

		assert isinstance(serviceref, ServiceReference)

		if serviceref and serviceref.isRecordable():
			self.service_ref = serviceref
		else:
			self.service_ref = ServiceReference(None)
		self.dontSave = False
		if not description or not name or not eit:
			evt = self.getEventFromEPG()
			if evt:
				if not description:
					description = evt.getShortDescription()
				if not description:
					description = evt.getExtendedDescription()
				if not name:
					name = evt.getEventName()
				if not eit:
					eit = evt.getEventId()
		self.eit = eit
		self.name = name
		self.description = description
		self.disabled = disabled
		self.timer = None
		self.__record_service = None
		self.start_prepare = 0
		self.justplay = justplay
		self.always_zap = always_zap
		self.afterEvent = afterEvent
		self.dirname = dirname
		self.dirnameHadToFallback = False
		self.autoincrease = False
		self.autoincreasetime = 3600 * 24  # 1 day
		self.tags = tags or []

		if descramble == 'notset' and record_ecm == 'notset':
			if config.recording.ecm_data.value == 'descrambled+ecm':
				self.descramble = True
				self.record_ecm = True
			elif config.recording.ecm_data.value == 'scrambled+ecm':
				self.descramble = False
				self.record_ecm = True
			elif config.recording.ecm_data.value == 'normal':
				self.descramble = True
				self.record_ecm = False
		else:
			self.descramble = descramble
			self.record_ecm = record_ecm

		self.rename_repeat = rename_repeat
		self.needChangePriorityFrontend = config.usage.recording_frontend_priority.value != "-2" and config.usage.recording_frontend_priority.value != config.usage.frontend_priority.value
		self.change_frontend = False
		self.isAutoTimer = isAutoTimer
		self.ice_timer_id = ice_timer_id
		self.wasInStandby = False

		self.log_entries = []
		self.resetState()

	def __repr__(self):
		ice = ""
		if self.ice_timer_id:
			ice = ", ice_timer_id=%s" % self.ice_timer_id
		disabled = ""
		if self.disabled:
			disabled = ", Disabled"
		return "RecordTimerEntry(name=%s, begin=%s, end=%s, serviceref=%s, justplay=%s, isAutoTimer=%s%s%s)" % (self.name, ctime(self.begin), ctime(self.end), self.service_ref, self.justplay, self.isAutoTimer, ice, disabled)

	def log(self, code, msg):
		self.log_entries.append((int(time()), code, msg))
		# print "[TIMER]", msg

	def freespace(self):
		self.MountPath = None
		if not self.dirname:
			dirname = findSafeRecordPath(defaultMoviePath())
		else:
			dirname = findSafeRecordPath(self.dirname)
			if dirname is None:
				dirname = findSafeRecordPath(defaultMoviePath())
				self.dirnameHadToFallback = True
		if not dirname:
			return False

		self.MountPath = dirname
		mountwriteable = os.access(dirname, os.W_OK)
		if not mountwriteable:
			self.log(0, ("Mount '%s' is not writeable." % dirname))
			return False

		s = os.statvfs(dirname)
		if (s.f_bavail * s.f_bsize) / 1000000 < 1024:
			self.log(0, "Not enough free space to record")
			return False
		else:
			self.log(0, "Found enough free space to record")
			return True

	def calculateFilename(self, name=None):
		service_name = self.service_ref.getServiceName()
		begin_date = strftime("%Y%m%d %H%M", localtime(self.begin))

		name = name or self.name
		filename = begin_date + " - " + service_name
		if name:
			if config.recording.filename_composition.value == "short":
				filename = strftime("%Y%m%d", localtime(self.begin)) + " - " + name
			elif config.recording.filename_composition.value == "long":
				filename += " - " + name + " - " + self.description
			else:
				filename += " - " + name  # standard

		if config.recording.ascii_filenames.value:
			filename = ASCIItranslit.legacyEncode(filename)

		self.Filename = Directories.getRecordingFilename(filename, self.MountPath)
		self.log(0, "Filename calculated as: '%s'" % self.Filename)
		return self.Filename

	def getEventFromEPG(self):
		epgcache = eEPGCache.getInstance()
		queryTime = self.begin + (self.end - self.begin) / 2
		ref = self.service_ref and self.service_ref.ref
		return epgcache.lookupEventTime(ref, queryTime)

	def tryPrepare(self):
		if self.justplay:
			return True
		else:
			if not self.calculateFilename():
				self.do_backoff()
				self.start_prepare = time() + self.backoff
				return False
			rec_ref = self.service_ref and self.service_ref.ref
			if rec_ref and rec_ref.flags & eServiceReference.isGroup:
				rec_ref = getBestPlayableServiceReference(rec_ref, eServiceReference())
				if not rec_ref:
					self.log(1, "'get best playable service for group... record' failed")
					return False

			self.setRecordingPreferredTuner()
			self.record_service = rec_ref and NavigationInstance.instance.recordService(rec_ref)

			if not self.record_service:
				self.log(1, "'record service' failed")
				self.setRecordingPreferredTuner(setdefault=True)
				return False

			name = self.name
			description = self.description
			if self.repeated:
				epgcache = eEPGCache.getInstance()
				queryTime = self.begin + (self.end - self.begin) / 2
				evt = epgcache.lookupEventTime(rec_ref, queryTime)
				if evt:
					if self.rename_repeat:
						event_description = evt.getShortDescription()
						if not event_description:
							event_description = evt.getExtendedDescription()
						if event_description and event_description != description:
							description = event_description
						event_name = evt.getEventName()
						if event_name and event_name != name:
							name = event_name
							if not self.calculateFilename(event_name):
								self.do_backoff()
								self.start_prepare = time() + self.backoff
								return False
					event_id = evt.getEventId()
				else:
					event_id = -1
			else:
				event_id = self.eit
				if event_id is None:
					event_id = -1

			prep_res = self.record_service.prepare(self.Filename + ".ts", self.begin, self.end, event_id, self.name.replace("\n", ""), self.description.replace("\n", ""), ' '.join(self.tags), bool(self.descramble), bool(self.record_ecm))
			if prep_res:
				if prep_res == -255:
					self.log(4, "failed to write meta information")
				else:
					self.log(2, "'prepare' failed: error %d" % prep_res)

				# We must calculate start time before stopRecordService call
				# because in Screens/Standby.py TryQuitMainloop tries to get
				# the next start time in evEnd event handler...
				self.do_backoff()
				self.start_prepare = time() + self.backoff

				NavigationInstance.instance.stopRecordService(self.record_service)
				self.record_service = None
				self.setRecordingPreferredTuner(setdefault=True)
				return False
			return True

	def do_backoff(self):
		if self.backoff == 0:
			self.backoff = 5
		else:
			self.backoff *= 2
			if self.backoff > 100:
				self.backoff = 100
		self.log(10, "backoff: retry in %d seconds" % self.backoff)

	def activate(self):
		next_state = self.state + 1
		self.log(5, "activating state %d" % next_state)

		if next_state == self.StatePrepared:
			if not self.justplay and not self.freespace():
				Notifications.AddPopup(
					text=_("Write error while recording. Disk full?\n%s") % self.name,
					type=MessageBox.TYPE_ERROR, timeout=5, id="DiskFullMessage")
				self.failed = True
				self.next_activation = time()
				self.end = time() + 5
				self.backoff = 0
				return True

			if self.always_zap:
				if Screens.Standby.inStandby:
					self.wasInStandby = True
					eActionMap.getInstance().bindAction('', -maxint - 1, self.keypress)
					# Set service to zap after standby
					Screens.Standby.inStandby.prev_running_service = self.service_ref.ref
					Screens.Standby.inStandby.paused_service = None
					# Wakeup standby
					Screens.Standby.inStandby.Power()
					self.log(5, "wakeup and zap to recording service")
				else:
					cur_zap_ref = NavigationInstance.instance.getCurrentlyPlayingServiceReference()
					if cur_zap_ref and not cur_zap_ref.getPath():  # Do not zap away if it is not a live service
						Notifications.AddNotification(MessageBox, _("In order to record a timer, the TV was switched to the recording service!\n"), type=MessageBox.TYPE_INFO, timeout=20)
						self.setRecordingPreferredTuner()
						self.failureCB(True)
						self.log(5, "zap to recording service")

			if self.tryPrepare():
				self.log(6, "prepare ok, waiting for begin")
				# Create file to "reserve" the filename
				# because another recording at the same time
				# on another service can try to record the same event
				# i.e. cable / sat.. then the second recording needs an own extension...
				# If we create the file
				# here then calculateFilename is kept happy
				if not self.justplay:
					open(self.Filename + ".ts", "w").close()
					# Give the Trashcan a chance to clean up
					try:
						Trashcan.instance.cleanIfIdle()
					except Exception, e:
						print "[TIMER] Failed to call Trashcan.instance.cleanIfIdle()"
						print "[TIMER] Error:", e
				# Fine. It worked, resources are allocated.
				self.next_activation = self.begin
				self.backoff = 0
				return True

			self.log(7, "prepare failed")
			if self.first_try_prepare:
				self.first_try_prepare = False
				cur_ref = NavigationInstance.instance.getCurrentlyPlayingServiceReference()
				if cur_ref and not cur_ref.getPath():
					if Screens.Standby.inStandby:
						self.setRecordingPreferredTuner()
						self.failureCB(True)
					elif not config.recording.asktozap.value:
						self.log(8, "asking user to zap away")
						Notifications.AddNotificationWithCallback(self.failureCB, MessageBox, _("A timer failed to record!\nDisable TV and try again?\n"), timeout=20)
					else:  # Zap without asking
						self.log(9, "zap without asking")
						Notifications.AddNotification(MessageBox, _("In order to record a timer, the TV was switched to the recording service!\n"), type=MessageBox.TYPE_INFO, timeout=20)
						self.setRecordingPreferredTuner()
						self.failureCB(True)
				elif cur_ref:
					self.log(8, "currently running service is not a live service.. so stop it makes no sense")
				else:
					self.log(8, "currently no service running... so we dont need to stop it")
			return False

		elif next_state == self.StateRunning:
			global wasRecTimerWakeup
			if os.path.exists("/tmp/was_rectimer_wakeup") and not wasRecTimerWakeup:
				wasRecTimerWakeup = int(open("/tmp/was_rectimer_wakeup", "r").read()) and True or False
				os.remove("/tmp/was_rectimer_wakeup")

			# If this timer has been cancelled or has failed,
			# just go to "end" state.
			if self.cancelled:
				return True

			if self.failed:
				return True

			if self.justplay:
				if Screens.Standby.inStandby:
					self.wasInStandby = True
					eActionMap.getInstance().bindAction('', -maxint - 1, self.keypress)
					self.log(11, "wakeup and zap")
					# Set service to zap after standby
					Screens.Standby.inStandby.prev_running_service = self.service_ref.ref
					Screens.Standby.inStandby.paused_service = None
					# Wakeup standby
					Screens.Standby.inStandby.Power()
				else:
					self.log(11, "zapping")
					NavigationInstance.instance.isMovieplayerActive()
					from Screens.ChannelSelection import ChannelSelection
					ChannelSelectionInstance = ChannelSelection.instance
					self.service_types = service_types_tv
					if ChannelSelectionInstance:
						if config.usage.multibouquet.value:
							bqrootstr = '1:7:1:0:0:0:0:0:0:0:FROM BOUQUET "bouquets.tv" ORDER BY bouquet'
						else:
							bqrootstr = '%s FROM BOUQUET "userbouquet.favourites.tv" ORDER BY bouquet' % self.service_types
						rootstr = ''
						serviceHandler = eServiceCenter.getInstance()
						rootbouquet = eServiceReference(bqrootstr)
						bouquet = eServiceReference(bqrootstr)
						bouquetlist = serviceHandler.list(bouquet)
						if bouquetlist is not None:
							while True:
								bouquet = bouquetlist.getNext()
								if bouquet.flags & eServiceReference.isDirectory:
									ChannelSelectionInstance.clearPath()
									ChannelSelectionInstance.setRoot(bouquet)
									servicelist = serviceHandler.list(bouquet)
									if servicelist is not None:
										serviceIterator = servicelist.getNext()
										while serviceIterator.valid():
											if self.service_ref.ref == serviceIterator:
												break
											serviceIterator = servicelist.getNext()
										if self.service_ref.ref == serviceIterator:
											break
							ChannelSelectionInstance.enterPath(rootbouquet)
							ChannelSelectionInstance.enterPath(bouquet)
							ChannelSelectionInstance.saveRoot()
							ChannelSelectionInstance.saveChannel(self.service_ref.ref)
						ChannelSelectionInstance.addToHistory(self.service_ref.ref)
					NavigationInstance.instance.playService(self.service_ref.ref)
				return True
			else:
				self.log(11, "start recording")
				record_res = self.record_service.start()
				self.setRecordingPreferredTuner(setdefault=True)
				if record_res:
					self.log(13, "start record returned %d" % record_res)
					self.do_backoff()
					# Retry
					self.begin = time() + self.backoff
					return False
				return True

		elif next_state == self.StateEnded or next_state == self.StateFailed:
			old_end = self.end
			if self.setAutoincreaseEnd():
				self.log(12, "autoincrease recording %d minute(s)" % int((self.end - old_end) / 60))
				self.state -= 1
				return True
			self.log(12, "stop recording")
			if not self.justplay:
				if self.record_service:
					NavigationInstance.instance.stopRecordService(self.record_service)
					self.record_service = None

			NavigationInstance.instance.RecordTimer.saveTimer()
			if self.afterEvent == AFTEREVENT.STANDBY or (not wasRecTimerWakeup and Screens.Standby.inStandby and self.afterEvent == AFTEREVENT.AUTO) or self.wasInStandby:
				self.keypress()  # This unbinds the keypress detection
				if not Screens.Standby.inStandby:  # Not already in standby
					Notifications.AddNotificationWithCallback(self.sendStandbyNotification, MessageBox, _("A finished record timer wants to set your\n%s %s to standby. Do that now?") % (getMachineBrand(), getMachineName()), timeout=180)
			elif self.afterEvent == AFTEREVENT.DEEPSTANDBY or (wasRecTimerWakeup and self.afterEvent == AFTEREVENT.AUTO):
				if (abs(NavigationInstance.instance.RecordTimer.getNextRecordingTime() - time()) <= 900 or abs(NavigationInstance.instance.RecordTimer.getNextZapTime() - time()) <= 900) or NavigationInstance.instance.RecordTimer.getStillRecording():
					print '[Timer] Recording or Recording due is next 15 mins, not return to deepstandby'
					return True
				if not Screens.Standby.inTryQuitMainloop:  # The shutdown messagebox is not open
					if Screens.Standby.inStandby:  # In standby
						quitMainloop(1)
					else:
						Notifications.AddNotificationWithCallback(self.sendTryQuitMainloopNotification, MessageBox, _("A finished record timer wants to shut down\nyour %s %s. Shutdown now?") % (getMachineBrand(), getMachineName()), timeout=180)
			return True

	def keypress(self, key=None, flag=1):
		if flag and self.wasInStandby:
			self.wasInStandby = False
			eActionMap.getInstance().unbindAction('', self.keypress)

	def setAutoincreaseEnd(self, entry=None):
		if not self.autoincrease:
			return False
		if entry is None:
			new_end = int(time()) + self.autoincreasetime
		else:
			new_end = entry.begin - 30

		dummyentry = RecordTimerEntry(
			self.service_ref, self.begin, new_end, self.name, self.description, self.eit, disabled=True,
			justplay=self.justplay, afterEvent=self.afterEvent, dirname=self.dirname, tags=self.tags)
		dummyentry.disabled = self.disabled
		timersanitycheck = TimerSanityCheck(NavigationInstance.instance.RecordTimer.timer_list, dummyentry)
		if not timersanitycheck.check():
			simulTimerList = timersanitycheck.getSimulTimerList()
			if simulTimerList is not None and len(simulTimerList) > 1:
				new_end = simulTimerList[1].begin
				new_end -= 30  # Allow 30 seconds preparation time
		if new_end <= time():
			return False
		self.end = new_end
		return True

	def setRecordingPreferredTuner(self, setdefault=False):
		if self.needChangePriorityFrontend:
			elem = None
			if not self.change_frontend and not setdefault:
				elem = config.usage.recording_frontend_priority.value
				self.change_frontend = True
			elif self.change_frontend and setdefault:
				elem = config.usage.frontend_priority.value
				self.change_frontend = False
			if elem is not None:
				setPreferredTuner(int(elem))

	def sendStandbyNotification(self, answer):
		if answer:
			Notifications.AddNotification(Screens.Standby.Standby)

	def sendTryQuitMainloopNotification(self, answer):
		if answer:
			Notifications.AddNotification(Screens.Standby.TryQuitMainloop, 1)
		else:
			global wasRecTimerWakeup
			wasRecTimerWakeup = False

	def getNextActivation(self):
		self.isStillRecording = False
		if self.state == self.StateEnded or self.state == self.StateFailed:
			if self.end > time():
				self.isStillRecording = True
			return self.end
		next_state = self.state + 1
		if next_state == self.StateEnded or next_state == self.StateFailed:
			if self.end > time():
				self.isStillRecording = True
		return {
			self.StatePrepared: self.start_prepare,
			self.StateRunning: self.begin,
			self.StateEnded: self.end
		}[next_state]

	def failureCB(self, answer):
		if answer:
			self.log(13, "ok, zapped away")
			# NavigationInstance.instance.stopUserServices()
			from Screens.ChannelSelection import ChannelSelection
			ChannelSelectionInstance = ChannelSelection.instance
			self.service_types = service_types_tv
			if ChannelSelectionInstance:
				if config.usage.multibouquet.value:
					bqrootstr = '1:7:1:0:0:0:0:0:0:0:FROM BOUQUET "bouquets.tv" ORDER BY bouquet'
				else:
					bqrootstr = '%s FROM BOUQUET "userbouquet.favourites.tv" ORDER BY bouquet' % self.service_types
				rootstr = ''
				serviceHandler = eServiceCenter.getInstance()
				rootbouquet = eServiceReference(bqrootstr)
				bouquet = eServiceReference(bqrootstr)
				bouquetlist = serviceHandler.list(bouquet)
				if bouquetlist is not None:
					while True:
						bouquet = bouquetlist.getNext()
						if bouquet.flags & eServiceReference.isDirectory:
							ChannelSelectionInstance.clearPath()
							ChannelSelectionInstance.setRoot(bouquet)
							servicelist = serviceHandler.list(bouquet)
							if servicelist is not None:
								serviceIterator = servicelist.getNext()
								while serviceIterator.valid():
									if self.service_ref.ref == serviceIterator:
										break
									serviceIterator = servicelist.getNext()
								if self.service_ref.ref == serviceIterator:
									break
					ChannelSelectionInstance.enterPath(rootbouquet)
					ChannelSelectionInstance.enterPath(bouquet)
					ChannelSelectionInstance.saveRoot()
					ChannelSelectionInstance.saveChannel(self.service_ref.ref)
				ChannelSelectionInstance.addToHistory(self.service_ref.ref)
			NavigationInstance.instance.playService(self.service_ref.ref)
		else:
			self.log(14, "user didn't want to zap away, record will probably fail")

	def timeChanged(self):
		old_prepare = self.start_prepare
		self.start_prepare = self.begin - self.prepare_time
		self.backoff = 0

		if int(old_prepare) > 60 and int(old_prepare) != int(self.start_prepare):
			self.log(15, "record time changed, start prepare is now: %s" % ctime(self.start_prepare))

	def gotRecordEvent(self, record, event):
		# TODO: this is not working (never true), please fix (comparing two swig wrapped ePtrs).
		if self.__record_service.__deref__() != record.__deref__():
			return
		# self.log(16, "record event %d" % event)
		if event == iRecordableService.evRecordWriteError:
			print "WRITE ERROR on recording, disk full?"
			# Show notification. The 'id' will make sure that it will be
			# displayed only once, even if multiple timers are failing at the
			# same time (which is likely in if the disk is full).
			Notifications.AddPopup(text=_("Write error while recording. Disk full?\n"), type=MessageBox.TYPE_ERROR, timeout=0, id="DiskFullMessage")
			# OK, the recording has been stopped. We need to properly record
			# that in our state, but also allow the possibility of a re-try.
			# TODO: This has to be done.
		elif event == iRecordableService.evStart:
			text = _("A recording has been started:\n%s") % self.name
			notify = config.usage.show_message_when_recording_starts.value and not Screens.Standby.inStandby and \
				Screens.InfoBar.InfoBar.instance and \
				Screens.InfoBar.InfoBar.instance.execing
			if self.dirnameHadToFallback:
				text = '\n'.join((text, _("Please note that the previously selected media could not be accessed and therefore the default directory is being used instead.")))
				notify = True
			if notify:
				Notifications.AddPopup(text=text, type=MessageBox.TYPE_INFO, timeout=3)
		elif event == iRecordableService.evRecordAborted:
			NavigationInstance.instance.RecordTimer.removeEntry(self)

	# We have record_service as property to automatically subscribe to record service events
	def setRecordService(self, service):
		if self.__record_service is not None:
			# print "[remove callback]"
			NavigationInstance.instance.record_event.remove(self.gotRecordEvent)

		self.__record_service = service

		if self.__record_service is not None:
			# print "[add callback]"
			NavigationInstance.instance.record_event.append(self.gotRecordEvent)

	record_service = property(lambda self: self.__record_service, setRecordService)

def createTimer(xml):
	begin = int(xml.get("begin"))
	end = int(xml.get("end"))
	serviceref = ServiceReference(xml.get("serviceref").encode("utf-8"))
	description = xml.get("description").encode("utf-8")
	repeated = xml.get("repeated").encode("utf-8")
	rename_repeat = long(xml.get("rename_repeat") or "1")
	disabled = long(xml.get("disabled") or "0")
	justplay = long(xml.get("justplay") or "0")
	always_zap = long(xml.get("always_zap") or "0")
	afterevent = str(xml.get("afterevent") or "nothing")
	afterevent = {
		"nothing": AFTEREVENT.NONE,
		"standby": AFTEREVENT.STANDBY,
		"deepstandby": AFTEREVENT.DEEPSTANDBY,
		"auto": AFTEREVENT.AUTO
	}[afterevent]
	eit = xml.get("eit")
	if eit and eit != "None":
		eit = long(eit)
	else:
		eit = None
	location = xml.get("location")
	if location and location != "None":
		location = location.encode("utf-8")
	else:
		location = None
	tags = xml.get("tags")
	if tags and tags != "None":
		tags = tags.encode("utf-8").split(' ')
	else:
		tags = None
	descramble = int(xml.get("descramble") or "1")
	record_ecm = int(xml.get("record_ecm") or "0")
	isAutoTimer = int(xml.get("isAutoTimer") or "0")
	ice_timer_id = xml.get("ice_timer_id")
	if ice_timer_id:
		ice_timer_id = ice_timer_id.encode("utf-8")
	name = xml.get("name").encode("utf-8")
	# filename = xml.get("filename").encode("utf-8")
	entry = RecordTimerEntry(
		serviceref, begin, end, name, description, eit, disabled, justplay, afterevent,
		dirname=location, tags=tags, descramble=descramble, record_ecm=record_ecm,
		isAutoTimer=isAutoTimer, ice_timer_id=ice_timer_id, always_zap=always_zap,
		rename_repeat=rename_repeat)
	entry.repeated = int(repeated)

	for l in xml.findall("log"):
		time = int(l.get("time"))
		code = int(l.get("code"))
		msg = l.text.strip().encode("utf-8")
		entry.log_entries.append((time, code, msg))

	return entry

class RecordTimer(timer.Timer):
	def __init__(self):
		timer.Timer.__init__(self)

		self.onTimerAdded = []
		self.onTimerRemoved = []
		self.onTimerChanged = []

		self.Filename = Directories.resolveFilename(Directories.SCOPE_CONFIG, "timers.xml")

		try:
			self.loadTimer()
		except IOError:
			print "unable to load timers from file!"

	def timeChanged(self, entry):
		timer.Timer.timeChanged(self, entry)
		for f in self.onTimerChanged:
			f(entry)

	def cleanup(self):
		for entry in self.processed_timers[:]:
			if not entry.disabled:
				self.processed_timers.remove(entry)
				for f in self.onTimerRemoved:
					f(entry)
		self.saveTimer()

	def doActivate(self, w):
		# If the timer should be skipped (e.g. disabled or
		# its end time has past), simply abort the timer.
		# Don't run through all the states.
		if w.shouldSkip():
			w.state = RecordTimerEntry.StateEnded
		else:
			# If active returns true, this means "accepted".
			# Otherwise, the current state is kept.
			# The timer entry itself will fix up the delay.
			if w.activate():
				w.state += 1

		try:
			self.timer_list.remove(w)
		except:
			print '[RecordTimer]: Remove list failed'

		# Did this timer reach the final state?
		if w.state < RecordTimerEntry.StateEnded:
			# No, sort it into active list
			insort(self.timer_list, w)
		else:
			# Yes. Process repeat if necessary, and re-add.
			if w.repeated:
				w.processRepeated()
				w.state = RecordTimerEntry.StateWaiting
				w.first_try_prepare = True
				self.addTimerEntry(w)
			else:
				# Check for disabled timers whose end time has passed
				self.cleanupDisabled()
				# Remove old timers as set in config
				self.cleanupDaily(config.recording.keep_timers.value)
				insort(self.processed_timers, w)
		self.stateChanged(w)

	def isRecTimerWakeup(self):
		return wasRecTimerWakeup

	def isRecording(self):
		isRunning = False
		for timer in self.timer_list:
			if timer.isRunning() and not timer.justplay:
				isRunning = True
		return isRunning

	def loadTimer(self):
		# TODO: PATH!
		if not Directories.fileExists(self.Filename):
			return
		try:
			f = open(self.Filename, 'r')
			doc = xml.etree.cElementTree.parse(f)
			f.close()
		except SyntaxError:
			from Tools.Notifications import AddPopup
			from Screens.MessageBox import MessageBox

			AddPopup(_("The timer file (timers.xml) is corrupt and could not be loaded."), type=MessageBox.TYPE_ERROR, timeout=0, id="TimerLoadFailed")

			print "timers.xml failed to load!"
			try:
				os.rename(self.Filename, self.Filename + "_old")
			except (IOError, OSError):
				print "renaming broken timer failed"
			return
		except IOError:
			print "timers.xml not found!"
			return

		root = doc.getroot()

		# Post a message if there are timer overlaps in the timer file
		checkit = True
		for timer in root.findall("timer"):
			newTimer = createTimer(timer)
			if (self.record(newTimer, True, dosave=False) is not None) and (checkit is True):
				from Tools.Notifications import AddPopup
				from Screens.MessageBox import MessageBox
				AddPopup(_("Timer overlap in timers.xml detected!\nPlease recheck it!"), type=MessageBox.TYPE_ERROR, timeout=0, id="TimerLoadFailed")
				checkit = False  # The message only needs to be displayed once

	def saveTimer(self):
		list = ['<?xml version="1.0" ?>\n', '<timers>\n']

		for timer in self.timer_list + self.processed_timers:
			if timer.dontSave:
				continue
			list.append('<timer')
			list.append(' begin="' + str(int(timer.begin)) + '"')
			list.append(' end="' + str(int(timer.end)) + '"')
			list.append(' serviceref="' + stringToXML(str(timer.service_ref)) + '"')
			list.append(' repeated="' + str(int(timer.repeated)) + '"')
			list.append(' rename_repeat="' + str(int(timer.rename_repeat)) + '"')
			list.append(' name="' + str(stringToXML(timer.name)) + '"')
			list.append(' description="' + str(stringToXML(timer.description)) + '"')
			list.append(' afterevent="' + str(stringToXML({
				AFTEREVENT.NONE: "nothing",
				AFTEREVENT.STANDBY: "standby",
				AFTEREVENT.DEEPSTANDBY: "deepstandby",
				AFTEREVENT.AUTO: "auto"
			}[timer.afterEvent])) + '"')
			if timer.eit is not None:
				list.append(' eit="' + str(timer.eit) + '"')
			if timer.dirname is not None:
				list.append(' location="' + str(stringToXML(timer.dirname)) + '"')
			if timer.tags is not None:
				list.append(' tags="' + str(stringToXML(' '.join(timer.tags))) + '"')
			list.append(' disabled="' + str(int(timer.disabled)) + '"')
			list.append(' justplay="' + str(int(timer.justplay)) + '"')
			list.append(' always_zap="' + str(int(timer.always_zap)) + '"')
			list.append(' descramble="' + str(int(timer.descramble)) + '"')
			list.append(' record_ecm="' + str(int(timer.record_ecm)) + '"')
			list.append(' isAutoTimer="' + str(int(timer.isAutoTimer)) + '"')
			if timer.ice_timer_id is not None:
				list.append(' ice_timer_id="' + str(timer.ice_timer_id) + '"')
			list.append('>\n')

			for time, code, msg in timer.log_entries:
				list.append('<log')
				list.append(' code="' + str(code) + '"')
				list.append(' time="' + str(time) + '"')
				list.append('>')
				list.append(str(stringToXML(msg)))
				list.append('</log>\n')

			list.append('</timer>\n')

		list.append('</timers>\n')

		try:
			f = open(self.Filename + ".writing", "w")
			for x in list:
				f.write(x)
			f.flush()

			os.fsync(f.fileno())
			f.close()
			os.rename(self.Filename + ".writing", self.Filename)
		except:
			print "There is not /etc/enigma2/timers.xml file !!! Why ?? "

	def getNextZapTime(self):
		now = time()
		for timer in self.timer_list:
			if not timer.justplay or timer.begin < now:
				continue
			return timer.begin
		return -1

	def getStillRecording(self):
		isStillRecording = False
		now = time()
		for timer in self.timer_list:
			if timer.isStillRecording:
				isStillRecording = True
				break
			elif abs(timer.begin - now) <= 10:
				isStillRecording = True
				break
		return isStillRecording

	def getNextRecordingTimeOld(self):
		now = time()
		for timer in self.timer_list:
			next_act = timer.getNextActivation()
			if timer.justplay or next_act < now:
				continue
			return next_act
		return -1

	def getNextRecordingTime(self):
		nextrectime = self.getNextRecordingTimeOld()
		faketime = time() + 300

		if config.timeshift.isRecording.value:
			if 0 < nextrectime < faketime:
				return nextrectime
			else:
				return faketime
		else:
			return nextrectime

	def isNextRecordAfterEventActionAuto(self):
		for timer in self.timer_list:
			if timer.justplay:
				continue
			if timer.afterEvent == AFTEREVENT.AUTO or timer.afterEvent == AFTEREVENT.DEEPSTANDBY:
				return True
		return False

	def record(self, entry, ignoreTSC=False, dosave=True):  # Called by loadTimer with dosave=False
		timersanitycheck = TimerSanityCheck(self.timer_list, entry)
		if not timersanitycheck.check():
			if not ignoreTSC:
				print "timer conflict detected!"
				return timersanitycheck.getSimulTimerList()
			else:
				print "ignore timer conflict"
		elif timersanitycheck.doubleCheck():
			print "ignore double timer"
			return None
		entry.timeChanged()
		# print "[Timer] Record " + str(entry)
		entry.Timer = self
		self.addTimerEntry(entry)
		if dosave:
			self.saveTimer()

		# Trigger onTimerAdded callbacks
		for f in self.onTimerAdded:
			f(entry)
		return None

	def isInTimer(self, eventid, begin, duration, service):
		returnValue = None
		kind = 0
		time_match = 0

		isAutoTimer = 0
		bt = None
		check_offset_time = not config.recording.margin_before.value and not config.recording.margin_after.value
		end = begin + duration
		refstr = ':'.join(service.split(':')[:11])
		for x in self.timer_list:
			isAutoTimer = 0
			if x.isAutoTimer == 1:
				isAutoTimer |= 1
			if x.ice_timer_id is not None:
				isAutoTimer |= 2
			check = ':'.join(x.service_ref.ref.toString().split(':')[:11]) == refstr
			if not check:
				sref = x.service_ref.ref
				parent_sid = sref.getUnsignedData(5)
				parent_tsid = sref.getUnsignedData(6)
				if parent_sid and parent_tsid:
					# Check for subservice
					sid = sref.getUnsignedData(1)
					tsid = sref.getUnsignedData(2)
					sref.setUnsignedData(1, parent_sid)
					sref.setUnsignedData(2, parent_tsid)
					sref.setUnsignedData(5, 0)
					sref.setUnsignedData(6, 0)
					check = sref.toCompareString() == refstr
					num = 0
					if check:
						check = False
						event = eEPGCache.getInstance().lookupEventId(sref, eventid)
						num = event and event.getNumOfLinkageServices() or 0
					sref.setUnsignedData(1, sid)
					sref.setUnsignedData(2, tsid)
					sref.setUnsignedData(5, parent_sid)
					sref.setUnsignedData(6, parent_tsid)
					for cnt in range(num):
						subservice = event.getLinkageService(sref, cnt)
						if sref.toCompareString() == subservice.toCompareString():
							check = True
							break
			if check:
				timer_end = x.end
				timer_begin = x.begin
				kind_offset = 0
				if not x.repeated and check_offset_time:
					if 0 < end - timer_end <= 59:
						timer_end = end
					elif 0 < timer_begin - begin <= 59:
						timer_begin = begin
				if x.justplay:
					kind_offset = 5
					if (timer_end - x.begin) <= 1:
						timer_end += 60
				if x.always_zap:
					kind_offset = 10

				if x.repeated != 0:
					if bt is None:
						bt = localtime(begin)
						bday = bt.tm_wday
						begin2 = 1440 + bt.tm_hour * 60 + bt.tm_min
						end2 = begin2 + duration / 60
					xbt = localtime(x.begin)
					xet = localtime(timer_end)
					offset_day = False
					checking_time = x.begin < begin or begin <= x.begin <= end
					if xbt.tm_yday != xet.tm_yday:
						oday = bday - 1
						if oday == -1:
							oday = 6
						offset_day = x.repeated & (1 << oday)
					xbegin = 1440 + xbt.tm_hour * 60 + xbt.tm_min
					xend = xbegin + ((timer_end - x.begin) / 60)
					if xend < xbegin:
						xend += 1440
					if x.repeated & (1 << bday) and checking_time:
						if begin2 < xbegin <= end2:
							if xend < end2:
								# Recording within event
								time_match = (xend - xbegin) * 60
								kind = kind_offset + 3
							else:
								# Recording last part of event
								time_match = (end2 - xbegin) * 60
								kind = kind_offset + 1
						elif xbegin <= begin2 <= xend:
							if xend < end2:
								# Recording first part of event
								time_match = (xend - begin2) * 60
								kind = kind_offset + 4
							else:
								# Recording whole event
								time_match = (end2 - begin2) * 60
								kind = kind_offset + 2
						elif offset_day:
							xbegin -= 1440
							xend -= 1440
							if begin2 < xbegin <= end2:
								if xend < end2:
									# Recording within event
									time_match = (xend - xbegin) * 60
									kind = kind_offset + 3
								else:
									# Recording last part of event
									time_match = (end2 - xbegin) * 60
									kind = kind_offset + 1
							elif xbegin <= begin2 <= xend:
								if xend < end2:
									# Recording first part of event
									time_match = (xend - begin2) * 60
									kind = kind_offset + 4
								else:
									# Recording whole event
									time_match = (end2 - begin2) * 60
									kind = kind_offset + 2
					elif offset_day and checking_time:
						xbegin -= 1440
						xend -= 1440
						if begin2 < xbegin <= end2:
							if xend < end2:
								# Recording within event
								time_match = (xend - xbegin) * 60
								kind = kind_offset + 3
							else:
								# Recording last part of event
								time_match = (end2 - xbegin) * 60
								kind = kind_offset + 1
						elif xbegin <= begin2 <= xend:
							if xend < end2:
								# Recording first part of event
								time_match = (xend - begin2) * 60
								kind = kind_offset + 4
							else:
								# Recording whole event
								time_match = (end2 - begin2) * 60
								kind = kind_offset + 2
				else:
					if begin < timer_begin <= end:
						if timer_end < end:
							# Recording within event
							time_match = timer_end - timer_begin
							kind = kind_offset + 3
						else:
							# Recording last part of event
							time_match = end - timer_begin
							kind = kind_offset + 1
					elif timer_begin <= begin <= timer_end:
						if timer_end < end:
							# Recording first part of event
							time_match = timer_end - begin
							kind = kind_offset + 4
						else:  # Recording whole event
							time_match = end - begin
							kind = kind_offset + 2

				if time_match:
					returnValue = (time_match, kind, isAutoTimer)
					if kind in (2, 7, 12):  # When full recording do not look further
						break
		return returnValue

	def removeEntry(self, entry):
		# print "[Timer] Remove " + str(entry)

		# Avoid re-enqueuing
		entry.repeated = False

		# Abort timer.
		# This sets the end time to current time, so timer will be stopped.
		entry.autoincrease = False
		entry.abort()

		if entry.state != entry.StateEnded:
			self.timeChanged(entry)

		# print "state: ", entry.state
		# print "in processed: ", entry in self.processed_timers
		# print "in running: ", entry in self.timer_list

		# Autoincrease instant timer if possible
		if not entry.dontSave:
			for x in self.timer_list:
				if x.setAutoincreaseEnd():
					self.timeChanged(x)
		# Now the timer should be in the processed_timers list.
		# Remove it from there.
		self.processed_timers.remove(entry)
		self.saveTimer()

		# Trigger onTimerRemoved callbacks
		for f in self.onTimerRemoved:
			f(entry)

	def shutdown(self):
		self.saveTimer()