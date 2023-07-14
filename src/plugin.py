#########################################################################################################
#                                                                                                       #
#  OpenATVbuildstatus: shows current build status of images and estimates time to next image build      #
#  Coded by Mr.Servo @ OpenATV (c) 2023                                                                 #
#  -----------------------------------------------------------------------------------------------------#
#  This plugin is licensed under the GNU version 3.0 <https://www.gnu.org/licenses/gpl-3.0.en.html>.    #
#  This plugin is NOT free software. It is open source, you are allowed to modify it (if you keep       #
#  the license), but it may not be commercially distributed. Advertise with this tool is not allowed.   #
#  For other uses, permission from the authors is necessary.                                            #
#                                                                                                       #
#########################################################################################################

# PYTHON IMPORTS
from os import makedirs
from os.path import join, exists
from requests import get, exceptions
from xml.etree.ElementTree import tostring, parse

# ENIGMA IMPORTS
from enigma import getDesktop, eTimer
from Components.ActionMap import ActionMap
from Components.config import config, ConfigSubsection, ConfigSelection, ConfigText, getConfigListEntry
from Components.ConfigList import ConfigListScreen
from Components.Label import Label
from Components.Sources.List import List
from Plugins.Plugin import PluginDescriptor
from Screens.Screen import Screen
from Screens.HelpMenu import HelpableScreen
from Screens.MessageBox import MessageBox
from Tools.LoadPixmap import LoadPixmap
from twisted.internet.reactor import callInThread

# PLUGIN IMPORTS
from . import PLUGINPATH, _  # for localized messages
from .Buildstatus import Buildstatus

# PLUGIN GLOBALS
BS = Buildstatus()
BS.start()

config.plugins.OpenATVstatus = ConfigSubsection()
config.plugins.OpenATVstatus.animate = ConfigSelection(default="50", choices=[("off", _("off")), ("70", _("slower")), ("50", _("normal")), ("30", _("faster"))])
config.plugins.OpenATVstatus.favarch = ConfigSelection(default="current", choices=[("current", _("from selected box"))] + BS.archlist)
config.plugins.OpenATVstatus.favboxes = ConfigText(default="", fixed_size=False)

VERSION = "0.2"
MODULE_NAME = __name__.split(".")[-1]
FAVLIST = [tuple(atom.strip() for atom in item.replace("(", "").replace(")", "").split(",")) for item in config.plugins.OpenATVstatus.favboxes.value.split(";")] if config.plugins.OpenATVstatus.favboxes.value else []
PICURL = "https://raw.githubusercontent.com/oe-alliance/remotes/master/boxes/"
TMPPATH = "/tmp/boxpictures/"


def readSkin(skin):
	skintext = ""
	skinfile = join(PLUGINPATH, "skin_%s.xml" % ("fHD" if getDesktop(0).size().width() > 1300 else "HD"))
	try:
		with open(skinfile, "r") as file:
			try:
				domskin = parse(file).getroot()
				for element in domskin:
					if element.tag == "screen" and element.attrib['name'] == skin:
						skintext = tostring(element).decode()
						break
			except Exception as err:
				print("[Skin] Error: Unable to parse skin data in '%s' - '%s'!" % (skinfile, err))
	except OSError as err:
		print("[Skin] Error: Unexpected error opening skin file '%s'! (%s)" % (skinfile, err))
	return skintext


class Carousel():
	def __init__(self, delay=50):
		self.delay = delay
		self.error = None
		self.stepcount = 0
		self.forward = True
		self.carouselTimer = None
		self.callactive = False
		self.prevstr = ""
		self.currstr = ""
		self.nextstr = ""

	def start(self, choicelist, index, callback):
		if not choicelist:
			self.error = "[%s] ERROR in module 'start': choicelist is empty or None!" % MODULE_NAME
			return
		index = index % len(choicelist)
		rlist = choicelist.copy()
		for i in range(3 - len(choicelist)):  # fill-up tiny rlists only
			rlist += choicelist
		self.callback = callback
		if index == 0:
			rlist = rlist[-1:] + rlist[:-1]  # rotate backward
		elif index > 1:
			for i in range(index - 1):
				rlist = rlist[1:] + rlist[:1]  # rotate forward
		self.rlist = rlist
		self.prevstr = rlist[0]
		self.currstr = rlist[1]
		self.nextstr = rlist[2]
		self.maxlen = max(len(self.prevstr), len(self.currstr), len(self.nextstr))

	def stop(self):
		self.callback = None
		self.setStandby()

	def setStandby(self):
		self.callactive = False
		if self.carouselTimer:
			self.carouselTimer.stop()

	def turnForward(self):  # pre-calculated constants to improve performance of 'self.turn'
		self.forward = True
		self.prevold = self.rlist[0]
		self.currold = self.rlist[1]
		self.nextold = self.rlist[2]
		self.rlist = self.rlist[1:] + self.rlist[:1]  # rotate forward
		self.prevnew = self.rlist[0]
		self.currnew = self.rlist[1]
		self.nextnew = self.rlist[2]
		self.setTimer()

	def turnBackward(self):  # pre-calculated constants to improve performance of 'self.turn'
		self.forward = False
		self.prevnew = self.rlist[0]
		self.currnew = self.rlist[1]
		self.nextnew = self.rlist[2]
		self.rlist = self.rlist[-1:] + self.rlist[:-1]  # rotate backward
		self.prevold = self.rlist[0]
		self.currold = self.rlist[1]
		self.nextold = self.rlist[2]
		self.setTimer()

	def setTimer(self):
		self.stepcount = 0
		self.callactive = True
		self.carouselTimer = eTimer()
		self.carouselTimer.callback.append(self.turn)
		self.carouselTimer.start(self.delay, False)

	def turn(self):  # rotates letters
		self.stepcount += 1
		step = self.stepcount if self.forward else -self.stepcount
		self.prevstr = "%s%s" % (self.prevold[step:], self.prevnew[:step])
		self.currstr = "%s%s" % (self.currold[step:], self.currnew[:step])
		self.nextstr = "%s%s" % (self.nextold[step:], self.nextnew[:step])
		if abs(step) > self.maxlen:
			self.setStandby()
		if self.callactive and self.callback:
			self.callback((self.prevstr, self.currstr, self.nextstr))


class ATVfavorites(Screen, HelpableScreen):
	def __init__(self, session):
		self.session = session
		self.skin = readSkin("ATVfavorites")
		Screen.__init__(self, session, self.skin)
		self.boxlist = []
		self.foundFavs = []
		self.platdict = dict()
		self.currindex = 0
		self.setTitle(_("Favorites"))
		self["platinfo"] = Label()
		self["key_red"] = Label(_("remove box from favorites"))
		self["key_blue"] = Label(_("Images list"))
		self["menu"] = List([])
		self["actions"] = ActionMap(["WizardActions",
				   					 "DirectionActions",
									 "MenuActions",
									"ColorActions"], {"ok": self.keyOk,
			   											"back": self.exit,
														'cancel': self.exit,
														"red": self.keyRed,
														"blue": self.keyBlue,
														"up": self.keyUp,
														"down": self.keyDown,
														"right": self.keyPageDown,
														"left": self.keyPageUp,
														"nextBouquet": self.keyPageDown,
														"prevBouquet": self.keyPageUp,
														"menu": self.openConfig,
													}, -1)
		self.onLayoutFinish.append(self.onLayoutFinished)
#		self.onClose.append(self.cleanup)
		makedirs(TMPPATH, exist_ok=True)

	def onLayoutFinished(self):
		self.createMenulist()
		self.refreshstatus()

	def createMenulist(self):
		boxlist = []
		usedarchs = []
		baselist = []
		piclist = []
		menulist = []
		self["menu"].setList([])
		if FAVLIST:
			self["menu"].style = "default"
			for favorite in FAVLIST:
				if favorite[1] not in usedarchs:
					usedarchs.append(favorite[1])
			for currarch in usedarchs:
				currplat = [plat for plat in BS.platlist if currarch.upper() in plat][0]
				BS.getbuildinfos(currplat)
				if BS.htmldict:
					for box in [item for item in FAVLIST if item[1] in set([item[1]])]:
						if box[1] in currarch and box[0] in BS.htmldict["boxinfo"]:
							boxlist.append((box[0], currarch))
							bd = BS.htmldict["boxinfo"][box[0]]
							palette = {"Building": 0x00B028, "Failed": 0xFF0400, "Complete": 0xFFFFFF, "Waiting": 0xFFAE00}
							color = palette.get(bd["BuildStatus"], 0xB0B0B0)
							buildtime, boxesahead, cycletime, counter, failed = BS.evaluate(box[0])
							if box[1] not in self.platdict:
								self.platdict[currplat] = dict()
								self.platdict[currplat]["cycletime"] = BS.strf_delta(cycletime)
								self.platdict[currplat]["boxcounter"] = counter
								self.platdict[currplat]["boxfailed"] = failed
							estimated = ""
							if buildtime:
								estimated = "%sh" % BS.strf_delta(buildtime)
								buildtime = "%sh" % buildtime
							else:
								buildtime = ""
							textlist = [box[0], box[1], bd["BuildStatus"], estimated, "%s" % boxesahead, bd["StartBuild"], bd["EndBuild"], buildtime, color]
							baselist.append(textlist)
							picfile = join(TMPPATH, "%s.png" % box[0])
							if exists(picfile):
								pixmap = LoadPixmap(cached=True, path=picfile)
							else:
								pixmap = None
								piclist.append(box[0])
							menulist.append(tuple(textlist + [pixmap]))
							self["menu"].updateList(menulist)
			self.baselist = baselist
			self.boxlist = boxlist
			for picname in piclist:
				callInThread(self.imageDownload, picname)
		else:
			self["menu"].style = "emptylist"
			self["menu"].updateList([(_("No favorites (box, platform) set yet."), _("Please select favorite(s) in the image lists."))])
		self["menu"].setIndex(self.currindex)

	def imageDownload(self, boxname):
		try:
			response = get(("%s%s.png" % (PICURL, boxname)).encode(), timeout=(3.05, 6))
			response.raise_for_status()
		except exceptions.RequestException as error:
			print("[%s] ERROR in module 'imageDownload': %s" % (MODULE_NAME, str(error)))
		else:
			with open(join(TMPPATH, "%s.png" % boxname), 'wb') as f:
				f.write(response.content)
		self.refreshMenulist()

	def refreshMenulist(self):
		menulist = []
		for textlist in self.baselist:
			menulist.append(tuple(textlist + [LoadPixmap(cached=True, path=join(TMPPATH, "%s.png" % textlist[0]))]))
		self["menu"].updateList(menulist)

	def refreshstatus(self):
		self.currindex = self["menu"].getSelectedIndex()
		if self.currindex:
			currplat = BS.getplatform(self.boxlist[self.currindex][1])
			platdict = self.platdict[currplat]
			self["platinfo"].setText("%s: %s, %s: %sh, %s %s, %s: %s" % (_("platform"), currplat, _("last build cycle"), platdict["cycletime"], platdict["boxcounter"], _("boxes"), _("failed"), platdict["boxfailed"]))

	def msgboxReturn(self, answer):
		if answer is True:
			FAVLIST.remove(self.foundFavs[0])
			config.plugins.OpenATVstatus.favboxes.value = ";".join("(%s)" % ",".join(item) for item in FAVLIST) if FAVLIST else ""
			config.plugins.OpenATVstatus.favboxes.save()
			self.session.open(MessageBox, text=_("Box '%s-%s' was sucessfully removed from favorites!") % self.boxlist[self.currindex], type=MessageBox.TYPE_INFO, timeout=2, close_on_any_key=True)
			self.createMenulist()

	def keyOk(self):
		pass

	def keyRed(self):
		self.foundFavs = [item for item in FAVLIST if item == self.boxlist[self.currindex]]
		if self.foundFavs:
			self.session.openWithCallback(self.msgboxReturn, MessageBox, _("Do you really want to remove Box '%s-%s' from favorites?") % self.boxlist[self.currindex], MessageBox.TYPE_YESNO, default=False)

	def keyBlue(self):
		favarch = config.plugins.OpenATVstatus.favarch.value
		currarch = favarch if favarch in BS.archlist or not self.boxlist else self.boxlist[self.currindex][1]
		currbox = self.boxlist[self.currindex] if self.boxlist else ""
		if currarch not in BS.archlist:
			currarch = BS.archlist[0]
		self.session.openWithCallback(self.createMenulist, ATVimageslist, (currbox, currarch))

	def keyUp(self):
		self["menu"].up()
		self.refreshstatus()

	def keyDown(self):
		self["menu"].down()
		self.refreshstatus()

	def keyPageUp(self):
		self["menu"].pageUp()
		self.refreshstatus()

	def keyPageDown(self):
		self["menu"].pageDown()
		self.refreshstatus()

	def keyTop(self):
		self["menu"].top()
		self.refreshstatus()

	def keyBottom(self):
		self["menu"].bottom()
		self.refreshstatus()

	def exit(self):
		BS.stop()
		self.close()

	def openConfig(self):
		self.session.open(BSconfig)


class ATVimageslist(Screen, HelpableScreen):
	def __init__(self, session, box):
		self.session = session
		self.currarch = box[1]
		self.currfav = box[0]
		self.skin = readSkin("ATVimageslist")
		Screen.__init__(self, session, self.skin)
		self.boxlist = []
		self.platidx = 0
		self.currindex = 0
		self.favindex = 0
		self.foundFavs = []
		self.setTitle(_("Images list"))
		self["prev_plat"] = Label()
		self["curr_plat"] = Label()
		self["next_plat"] = Label()
		self["prev_label"] = Label()
		self["curr_label"] = Label()
		self["next_label"] = Label()
		self["boxinfo"] = Label()
		self["platinfo"] = Label()
		self["menu"] = List([])
		self["key_red"] = Label()
		self["key_green"] = Label(_("jump to construction site"))
		self["key_yellow"] = Label(_("jump to favorite(s)"))
		self["key_menu"] = Label(_("Settings"))
		self["actions"] = ActionMap(["WizardActions",
				   					 "DirectionActions",
									 "MenuActions",
									 "ChannelSelectBaseActions",
									 "ColorActions"], {"ok": self.keyOk,
														"back": self.exit,
														"cancel": self.exit,
														"red": self.keyRed,
														"green": self.keyGreen,
														"yellow": self.keyYellow,
														"up": self.keyUp,
														"down": self.keyDown,
														"right": self.keyPageDown,
														"left": self.keyPageUp,
														"nextBouquet": self.keyPageDown,
														"prevBouquet": self.keyPageUp,
														"nextMarker": self.nextPlatform,
														"prevMarker": self.prevPlatform,
														"menu": self.openConfig,
													}, -1)
		self.platidx = BS.archlist.index(self.currarch)
		self.CS = Carousel(delay=int(config.plugins.OpenATVstatus.animate.value))
		self.CS.start(BS.platlist, self.platidx, self.CarouselCallback)
		self.onLayoutFinish.append(self.onLayoutFinished)

	def onLayoutFinished(self):
		self.setPlatformStatic()
		self.refreshplatlist()

	def refreshplatlist(self):
		self.currarch = BS.archlist[self.platidx]
		BS.getbuildinfos(BS.platlist[self.platidx], self.makeimagelist)

	def makeimagelist(self):
		self["prev_label"].setText(_("previous"))
		self["curr_label"].setText(_("current platform"))
		self["next_label"].setText(_("next"))
		menulist = []
		boxlist = []
		if BS.htmldict:
			for boxname in BS.htmldict["boxinfo"]:
				boxlist.append((boxname, self.currarch))
				bd = BS.htmldict["boxinfo"][boxname]
				palette = {"Building": 0x00B028, "Failed": 0xFF0400, "Complete": 0xB0B0B0, "Waiting": 0xFFAE00}
				color = 0xFDFf00 if [item for item in FAVLIST if item == (boxname, self.currarch)] else palette.get(bd["BuildStatus"], 0xB0B0B0)
				menulist.append(tuple([boxname, bd["BuildStatus"], bd["StartBuild"], bd["StartFeedSync"], bd["EndBuild"], bd["SyncTime"], bd["BuildTime"], color]))
			self["menu"].setList(menulist)
			self.boxlist = boxlist
		if self.currfav:
			self["menu"].setIndex(self.boxlist.index(self.currfav))
			self.currfav = None
		self.refreshstatus()

	def refreshstatus(self):
		self.currindex = self["menu"].getSelectedIndex()
		if [item for item in FAVLIST if item == self.boxlist[self.currindex]]:
			self["key_red"].setText(_("remove box from favorites"))
		else:
			self["key_red"].setText(_("add box to favorites"))
		buildtime, boxesahead, cycletime, counter, failed = BS.evaluate(self.boxlist[self.currindex][0])
		print("#####buildtime:", buildtime)
		if buildtime:
			estimated = BS.strf_delta(buildtime)
			self["boxinfo"].setText(_("next build ends in %sh, still %s boxes before") % (estimated, boxesahead))
		else:
			self["boxinfo"].setText(_("image is under construction or failed, duration is unclear..."))
		if cycletime:
			self["platinfo"].setText("%s: %sh, %s %s, %s: %s" % (_("last build cycle"), BS.strf_delta(cycletime), counter, _("boxes"), _("failed"), failed))
		else:
			self["boxinfo"].setText(_("no box found in this platform!"))
			self["platinfo"].setText(_("nothing to do - no build cycle"))
			self["menu"].setList([])

	def nextPlatform(self):
		self.platidx = (self.platidx + 1) % len(BS.platlist)
		if config.plugins.OpenATVstatus.animate.value == "off":
			self.setPlatformStatic()
		else:
			self.CS.turnForward()
		self.refreshplatlist()

	def prevPlatform(self):
		self.platidx = (self.platidx - 1) % len(BS.platlist)
		if config.plugins.OpenATVstatus.animate.value == "off":
			self.setPlatformStatic()
		else:
			self.CS.turnBackward()
		self.refreshplatlist()

	def setPlatformStatic(self):
		self["prev_plat"].setText(BS.platlist[self.platidx - 1] if self.platidx > 0 else BS.platlist[len(BS.platlist) - 1])
		self["curr_plat"].setText(BS.platlist[self.platidx])
		self["next_plat"].setText(BS.platlist[self.platidx + 1] if self.platidx < len(BS.platlist) - 1 else BS.platlist[0])

	def CarouselCallback(self, rotated):
		self["prev_plat"].setText(rotated[0])
		self["curr_plat"].setText(rotated[1])
		self["next_plat"].setText(rotated[2])

	def keyOk(self):
		pass

	def msgboxReturn(self, answer):
		if answer is True:
			FAVLIST.remove(self.foundFavs[0])
			config.plugins.OpenATVstatus.favboxes.value = ";".join("(%s)" % ",".join(item) for item in FAVLIST) if FAVLIST else ""
			config.plugins.OpenATVstatus.favboxes.save()
			self.session.open(MessageBox, text=_("Box '%s-%s' was sucessfully removed from favorites!") % self.boxlist[self.currindex], type=MessageBox.TYPE_INFO, timeout=2, close_on_any_key=True)
			self.refreshplatlist()

	def keyRed(self):
		self.foundFavs = [item for item in FAVLIST if item == self.boxlist[self.currindex]]
		if self.foundFavs:
			self.session.openWithCallback(self.msgboxReturn, MessageBox, _("Do you really want to remove Box '%s-%s' from favorites?") % self.boxlist[self.currindex], MessageBox.TYPE_YESNO, default=False)
		else:
			FAVLIST.append(self.boxlist[self.currindex])
			config.plugins.OpenATVstatus.favboxes.value = ";".join("(%s)" % ",".join(item) for item in FAVLIST) if FAVLIST else ""
			config.plugins.OpenATVstatus.favboxes.save()
			self.session.open(MessageBox, text=_("Box '%s-%s' was sucessfully added to favorites!") % self.boxlist[self.currindex], type=MessageBox.TYPE_INFO, timeout=2, close_on_any_key=True)
			self.refreshplatlist()

	def keyGreen(self):
		if self.boxlist:
			findbuildbox = (BS.findbuildbox(), self.currarch)
			if findbuildbox[0]:
				self["menu"].setIndex(self.boxlist.index(findbuildbox))
				self.refreshstatus()
			else:
				self.session.open(MessageBox, text=_("At the moment no image is built on the platform '%s'!") % BS.getplatform(self.currarch), type=MessageBox.TYPE_INFO, timeout=2, close_on_any_key=True)

	def keyYellow(self):
		if self.boxlist and FAVLIST:
			self.favindex = (self.favindex + 1) % len(FAVLIST)
			self.currfav = FAVLIST[self.favindex]
			if self.currfav in self.boxlist:
				self["menu"].setIndex(self.boxlist.index(self.currfav))
				self.refreshstatus()
			else:
				self.platidx = BS.archlist.index(self.currfav[1])
				self.setPlatformStatic()
				self.refreshplatlist()

	def keyUp(self):
		self["menu"].up()
		self.refreshstatus()

	def keyDown(self):
		self["menu"].down()
		self.refreshstatus()

	def keyPageUp(self):
		self["menu"].pageUp()
		self.refreshstatus()

	def keyPageDown(self):
		self["menu"].pageDown()
		self.refreshstatus()

	def keyTop(self):
		self["menu"].top()
		self.refreshstatus()

	def keyBottom(self):
		self["menu"].bottom()
		self.refreshstatus()

	def exit(self):
		BS.stop()
		self.CS.stop()
		self.close()

	def openConfig(self):
		self.session.open(BSconfig)


class ATVboxdetails(Screen, HelpableScreen):
	def __init__(self, session):
		self.session = session
		self.skin = readSkin("ATVboxdetails")
		Screen.__init__(self, session, self.skin)
		self.setTitle(_("Boxdetails"))
		self["actions"] = ActionMap(["WizardActions",
				   					 "DirectionActions",
									 "MenuActions"], {"ok": self.keyOk,
			   											"back": self.exit,
														'cancel': self.exit,
														"red": self.keyRed,
														"up": self.keyUp,
														"down": self.keyDown,
														"right": self.keyPageDown,
														"left": self.keyPageUp,
														"nextBouquet": self.keyPageDown,
														"prevBouquet": self.keyPageUp,
														#"menu": self.openMainMenu,
													}, -1)
		self.onLayoutFinish.append(self.onLayoutFinished)
#		self.onClose.append(self.cleanup)

	def onLayoutFinished(self):
		self["menu"].setList(FAVLIST)


class BSconfig(ConfigListScreen, Screen):
	def __init__(self, session):
		skin = readSkin("BSconfig")
		self.skin = skin
		Screen.__init__(self, session, skin)
		self.setTitle(_("Settings"))
		self['actions'] = ActionMap(['OkCancelActions', 'ColorActions'], {'cancel': self.keyCancel,
																		  'red': self.keyCancel,
																		  'green': self.keyGreen
																		  }, -2)
		clist = []
		ConfigListScreen.__init__(self, clist)
		clist.append(getConfigListEntry(_("Preferred box architecture for images list:"), config.plugins.OpenATVstatus.favarch, _("Specify which box architecture should be preferred when images list will be called. If option 'current' is selected, the architecture of the selected box is taken.")))
		clist.append(getConfigListEntry(_("Animation for change of platform:"), config.plugins.OpenATVstatus.animate, _("Sets the animation speed for the carousel function when changing platforms.")))
		self["config"].setList(clist)
		self["key_red"] = Label(_("cancel"))
		self["key_green"] = Label(_("save settings"))

	def keyGreen(self):
		config.plugins.OpenATVstatus.save()
		self.close()

	def keyCancel(self):
		for x in self['config'].list:
			x[1].cancel()
		self.close()


def main(session, **kwargs):
		session.open(ATVfavorites)


def autostart(reason, **kwargs):
	pass


def Plugins(**kwargs):
	return PluginDescriptor(name="OpenATV Status", icon='plugin.png', description=_("Current overview of the OpenATV images building servers"), where=PluginDescriptor.WHERE_PLUGINMENU, fnc=main)